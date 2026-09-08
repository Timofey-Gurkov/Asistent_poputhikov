"""
Приложение поиска попутчиков ГУК УрФУ <-> НВК
=================================================

Как запустить:
    pip install flet
    python app.py

После запуска приложение поднимет локальный веб-сервер и само откроет
браузер по адресу http://127.0.0.1:8550 (можно открыть эту ссылку на
нескольких вкладках/устройствах в одной сети — например, водитель на
одном телефоне, попутчик на другом).

Архитектура нарочно разделена на два слоя:
  1) TripStore — слой хранения данных (сейчас — обычный список в
                    памяти процесса, никаких баз данных).
  2) UI (main) — слой интерфейса на Flet, который обращается к
                    TripStore только через его публичные методы.

Такое разделение сделано специально для дальнейшего перехода на BLE:
когда понадобится передавать заявки не через общий Wi-Fi/интернет,
а по Bluetooth Low Energy между устройствами, достаточно будет
написать класс BLETripStore с теми же методами (add_trip,
get_active_trips, accept_trip, start_passenger, finish_trip,
get_trip) и подставить его вместо TripStore — код экранов
трогать не придётся.

В шапке приложения есть переключатель «Онлайн» / «Офлайн» — в обоих
положениях экран обновляется автоматически, разница лишь в механизме:
  • Онлайн (по умолчанию) — обновление приходит мгновенно пуш-уведомлением
    через page.pubsub, как только в сети появляются новые поездки/отклики.
  • Офлайн — пуш-уведомления не используются; вместо них фоновый поток
    раз в 2 секунды сам опрашивает локальное хранилище (store) и
    перерисовывает экран при изменениях. Ручного нажатия кнопки
    «Обновить» не требуется ни в одном из режимов — она оставлена
    просто как способ обновить мгновенно, не дожидаясь таймера.
"""
import asyncio
import re
import threading
import uuid

import flet as ft

# --------------------------------------------------------------------------
# 1. Слой хранения данных (легко заменить на BLE в будущем)
# --------------------------------------------------------------------------

LOCATIONS = ["ГУК УрФУ", "НВК"]

# ---- Валидация полей формы водителя --------------------------------------
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
PHONE_RE = re.compile(r"^(\+7|8|7)\d{10}$")
TELEGRAM_RE = re.compile(r"^@[A-Za-z0-9_]{5,32}$")


def validate_time(value: str) -> bool:
    """Время в формате ЧЧ:ММ, например 9:00 или 18:30."""
    return bool(TIME_RE.match((value or "").strip()))


def validate_price(value: str) -> bool:
    """Цена — положительное число (допускается дробное через точку/запятую)."""
    text = (value or "").strip().replace(",", ".")
    try:
        price = float(text)
    except ValueError:
        return False
    return price > 0


def validate_contact(value: str) -> bool:
    """Телефон (+7XXXXXXXXXX / 8XXXXXXXXXX) или телеграм-юзернейм (@name,
    5-32 символа: латиница, цифры, подчёркивание)."""
    text = (value or "").strip()
    digits_only = re.sub(r"[\s\-()]", "", text)
    return bool(PHONE_RE.match(digits_only) or TELEGRAM_RE.match(text))


# Российский автомобильный номер: буква, 3 цифры, 2 буквы, код региона
# (2-3 цифры). Буквы — только те кириллические, что совпадают по
# начертанию с латинскими (как и требует ГОСТ на автономера).
PLATE_RE = re.compile(r"^[АВЕКМНОРСТУХ]\d{3}[АВЕКМНОРСТУХ]{2}\d{2,3}$")


def normalize_plate(value: str) -> str:
    return (value or "").strip().upper().replace(" ", "").replace("Ё", "Е")


def validate_plate(value: str) -> bool:
    """Формат вида А111АА777 (буква-цифры-буквы-буквы-регион)."""
    return bool(PLATE_RE.match(normalize_plate(value)))


def format_route(trip: dict) -> str:
    return f"{trip['from']} → {trip['to']}"


class TripStore:
    """Хранит все активные поездки в обычном списке в оперативной памяти.

    Никакой базы данных нет: при перезапуске приложения все поездки
    пропадают, что полностью соответствует требованию "без БД".
    """

    def __init__(self):
        self._trips = []  # список словарей-поездок
        self._lock = threading.Lock()

    # ---- запись -----------------------------------------------------
    def add_trip(self, trip: dict) -> None:
        with self._lock:
            self._trips.append(trip)

    def accept_trip(self, trip_id: str, passenger_name: str):
        """Попутчик принимает поездку. Возвращает поездку либо None,
        если мест не осталось или поездка не найдена.

        Каждый принявший попутчик получает свой уникальный id — по нему
        водитель потом сможет начать поездку именно для этого человека,
        даже если у нескольких попутчиков совпадают имена."""
        with self._lock:
            for trip in self._trips:
                if trip["id"] == trip_id and trip["seats_available"] > 0:
                    trip["seats_available"] -= 1
                    trip["accepted"].append({
                        "id": uuid.uuid4().hex[:8],
                        "name": passenger_name,
                    })
                    return dict(trip)
        return None

    def start_passenger(self, trip_id: str, passenger_id: str) -> bool:
        """Водитель начинает поездку для конкретного попутчика: тот
        убирается из списка ожидающих посадки. Если это был последний
        ожидающий попутчик, вся поездка считается начатой и её данные
        удаляются полностью. Возвращает True, если поездка была удалена."""
        with self._lock:
            for trip in self._trips:
                if trip["id"] == trip_id:
                    trip["accepted"] = [
                        p for p in trip["accepted"] if p["id"] != passenger_id
                    ]
                    if not trip["accepted"]:
                        self._trips.remove(trip)
                        return True
                    return False
        return False

    def finish_trip(self, trip_id: str) -> None:
        """Полностью удаляет все данные о поездке (отмена поездки
        водителем)."""
        with self._lock:
            self._trips[:] = [t for t in self._trips if t["id"] != trip_id]

    # ---- чтение -------------------------------------------------------
    def get_active_trips(self):
        with self._lock:
            return [dict(t) for t in self._trips]

    def get_trip(self, trip_id: str):
        with self._lock:
            for trip in self._trips:
                if trip["id"] == trip_id:
                    return dict(trip)
        return None


store = TripStore()
PUBSUB_TOPIC = "trips_updated"


# --------------------------------------------------------------------------
# 2. Слой интерфейса (Flet)
# --------------------------------------------------------------------------

def main(page: ft.Page):
    page.title = "Попутчики: ГУК УрФУ <-> НВК"
    page.window_width = 420
    page.window_height = 780
    page.horizontal_alignment = ft.CrossAxisAlignment.CENTER
    page.scroll = ft.ScrollMode.AUTO
    page.padding = 20

    # состояние текущей сессии (у каждого подключившегося браузера — своё)
    state = {
        "role": None,  # "driver" | "passenger"
        "trip_id": None,  # id поездки, созданной этим водителем
        "passenger_name": None,
        "revealed_trip_id": None,  # id поездки, чей контакт уже раскрыт попутчику
        "passenger_list_view": None,  # активный ListView экрана списка поездок (если открыт)
        "online_mode": True,  # вкл — автообновление по сети; выкл — только вручную (офлайн)
    }

    content = ft.Column(spacing=16, width=380)

    # ---------------- Постоянная шапка: переключатель онлайн/офлайн ------
    # Живёт отдельно от `content` и не пересоздаётся при смене экранов,
    # поэтому переключатель виден всегда и не мигает при перерисовке.
    online_icon = ft.Icon(ft.Icons.WIFI, color=ft.Colors.GREEN_600, size=18)
    online_label = ft.Text("Онлайн", size=12, color=ft.Colors.GREEN_600)

    def toggle_online(e):
        state["online_mode"] = e.control.value
        if state["online_mode"]:
            online_icon.name = ft.Icons.WIFI
            online_icon.color = ft.Colors.GREEN_600
            online_label.value = "Онлайн"
            online_label.color = ft.Colors.GREEN_600
        else:
            online_icon.name = ft.Icons.WIFI_OFF
            online_icon.color = ft.Colors.GREY_500
            online_label.value = "Офлайн"
            online_label.color = ft.Colors.GREY_500
        header.update()
        snack(
            "Автообновление по сети включено: новые поездки и отклики появляются мгновенно"
            if state["online_mode"]
            else "Офлайн-режим: экран обновляется автоматически каждые пару секунд"
        )

    online_switch = ft.Switch(value=True, on_change=toggle_online, scale=0.85)

    header = ft.Row(
        [
            ft.Text("Попутчики УрФУ", size=16, weight=ft.FontWeight.BOLD),
            ft.Row([online_icon, online_label, online_switch], spacing=4),
        ],
        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        width=380,
    )

    page.add(header, ft.Divider(height=1), content)

    def render(controls):
        content.controls.clear()
        content.controls.extend(controls)
        page.update()

    def snack(text: str):
        page.snack_bar = ft.SnackBar(ft.Text(text), open=True)
        page.update()

    def safe_pubsub_send(topic):
        """Безопасная отправка pubsub-сообщения.
        В некоторых средах (например, при сборке через flet build / serious_python)
        event loop для pubsub может быть не инициализирован, что вызывает
        RuntimeError. В таком случае просто игнорируем ошибку, так как
        синхронизация между сессиями всё равно недоступна."""
        try:
            page.pubsub.send_all(topic)
        except RuntimeError:
            pass

    # ---------------- Экран выбора роли -------------------------------
    def show_role_selection(message: str | None = None):
        state["role"] = None
        state["trip_id"] = None
        state["revealed_trip_id"] = None
        state["passenger_list_view"] = None

        controls = [
            ft.Text("Попутчики УрФУ", size=26, weight=ft.FontWeight.BOLD),
            ft.Text("ГУК УрФУ ⇄ НВК", size=16, color=ft.Colors.GREY_600),
        ]
        if message:
            controls.append(
                ft.Container(
                    content=ft.Text(message, color=ft.Colors.WHITE),
                    bgcolor=ft.Colors.GREEN_600,
                    padding=10,
                    border_radius=8,
                )
            )
        controls += [
            ft.Text("Кто вы?", size=18),
            ft.ElevatedButton(
                "Я водитель",
                icon=ft.Icons.DIRECTIONS_CAR,
                width=300,
                on_click=lambda e: show_driver_form(),
            ),
            ft.ElevatedButton(
                "Я попутчик",
                icon=ft.Icons.PERSON,
                width=300,
                on_click=lambda e: ask_passenger_name(),
            ),
        ]
        render(controls)

    # ---------------- Водитель: форма создания поездки -----------------
    def show_driver_form():
        state["role"] = "driver"
        state["passenger_list_view"] = None

        driver_name_tf = ft.TextField(label="Ваше имя", width=300)
        from_tf = ft.TextField(label="Откуда", width=300, value=LOCATIONS[0])
        to_tf = ft.TextField(label="Куда", width=300, value=LOCATIONS[1])
        seats_dd = ft.Dropdown(
            label="Количество мест",
            width=300,
            options=[ft.dropdown.Option(str(n)) for n in range(1, 9)],
            value="1",
        )
        time_tf = ft.TextField(label="Время отправления, например 18:30", width=300)
        price_tf = ft.TextField(label="Цена, ₽", width=300, keyboard_type=ft.KeyboardType.NUMBER)
        contact_tf = ft.TextField(label="Контакт: телефон +7... или телеграм @username", width=300)
        plate_tf = ft.TextField(label="Номер машины, например А111АА777", width=300)
        comment_tf = ft.TextField(
            label="Комментарий (необязательно)",
            width=300,
            multiline=True,
            min_lines=1,
            max_lines=3,
        )
        error_text = ft.Text("", color=ft.Colors.RED_600)

        def create_trip(e):
            if not driver_name_tf.value or not driver_name_tf.value.strip():
                error_text.value = "Введите ваше имя"
                page.update()
                return

            if not from_tf.value or not from_tf.value.strip():
                error_text.value = "Укажите пункт «Откуда»"
                page.update()
                return

            if not to_tf.value or not to_tf.value.strip():
                error_text.value = "Укажите пункт «Куда»"
                page.update()
                return

            if from_tf.value.strip().lower() == to_tf.value.strip().lower():
                error_text.value = "Пункты «откуда» и «куда» должны отличаться"
                page.update()
                return

            try:
                seats = int(seats_dd.value)
            except (TypeError, ValueError):
                error_text.value = "Укажите количество мест"
                page.update()
                return

            if not validate_time(time_tf.value):
                error_text.value = "Время укажите в формате ЧЧ:ММ, например 18:30"
                page.update()
                return

            if not validate_price(price_tf.value):
                error_text.value = "Цена должна быть положительным числом"
                page.update()
                return

            if not validate_contact(contact_tf.value):
                error_text.value = (
                    "Контакт: телефон вида +79001234567/89001234567 "
                    "или телеграм-юзернейм вида @ivan_ivanov"
                )
                page.update()
                return

            if not validate_plate(plate_tf.value):
                error_text.value = "Номер машины укажите в формате А111АА777"
                page.update()
                return

            trip = {
                "id": uuid.uuid4().hex,
                "driver_name": driver_name_tf.value.strip(),
                "from": from_tf.value.strip(),
                "to": to_tf.value.strip(),
                "seats_total": seats,
                "seats_available": seats,
                "time": time_tf.value.strip(),
                "price": price_tf.value.strip(),
                "contact": contact_tf.value.strip(),
                "car_plate": normalize_plate(plate_tf.value),
                "comment": (comment_tf.value or "").strip(),
                "accepted": [],
            }
            store.add_trip(trip)
            state["trip_id"] = trip["id"]
            safe_pubsub_send(PUBSUB_TOPIC)
            show_driver_waiting()

        render([
            ft.Row([
                ft.IconButton(ft.Icons.ARROW_BACK, on_click=lambda e: show_role_selection()),
                ft.Text("Создание поездки", size=20, weight=ft.FontWeight.BOLD),
            ]),
            driver_name_tf,
            from_tf,
            to_tf,
            seats_dd,
            time_tf,
            price_tf,
            contact_tf,
            plate_tf,
            comment_tf,
            error_text,
            ft.ElevatedButton("Опубликовать поездку", icon=ft.Icons.CHECK, on_click=create_trip, width=300),
        ])

    # ---------------- Водитель: экран ожидания попутчиков ---------------
    def show_driver_waiting():
        trip = store.get_trip(state["trip_id"])
        if trip is None:
            show_role_selection("Поездка завершена")
            return

        def cancel_trip(e):
            store.finish_trip(trip["id"])
            safe_pubsub_send(PUBSUB_TOPIC)
            show_role_selection("Поездка отменена")

        def make_start_handler(passenger_id, passenger_name):
            def handler(e):
                trip_removed = store.start_passenger(trip["id"], passenger_id)
                safe_pubsub_send(PUBSUB_TOPIC)
                if trip_removed:
                    show_role_selection("Поездка начата. Хорошей дороги!")
                else:
                    snack(f"Поездка начата для: {passenger_name}")
                    show_driver_waiting()

            return handler

        if trip["accepted"]:
            accepted_rows = [
                ft.Row(
                    [
                        ft.Text(passenger["name"], size=15, expand=True),
                        ft.ElevatedButton(
                            "Поездка начата",
                            icon=ft.Icons.PLAY_ARROW,
                            bgcolor=ft.Colors.GREEN_600,
                            color=ft.Colors.WHITE,
                            on_click=make_start_handler(passenger["id"], passenger["name"]),
                        ),
                    ],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                )
                for passenger in trip["accepted"]
            ]
            accepted_section = ft.Column(accepted_rows, spacing=10)
        else:
            accepted_section = ft.Text("Пока никто не откликнулся", color=ft.Colors.GREY_600)

        controls = [
            ft.Row([
                ft.IconButton(ft.Icons.ARROW_BACK, on_click=lambda e: cancel_trip(e)),
                ft.Text("Ваша поездка", size=20, weight=ft.FontWeight.BOLD),
                ft.IconButton(ft.Icons.REFRESH, on_click=lambda e: show_driver_waiting()),
            ]),
            ft.Card(content=ft.Container(
                padding=16,
                content=ft.Column([
                    ft.Text(f"Водитель: {trip['driver_name']}", size=18, weight=ft.FontWeight.BOLD),
                    ft.Text(format_route(trip), size=16),
                    ft.Text(f"Время: {trip['time']}"),
                    ft.Text(f"Цена: {trip['price']} ₽"),
                    ft.Text(f"Машина: {trip['car_plate']}"),
                    ft.Text(f"Свободно мест: {trip['seats_available']} из {trip['seats_total']}"),
                    *([ft.Text(f"Комментарий: {trip['comment']}")] if trip.get("comment") else []),
                ]),
            )),
            ft.Text("Отклики (нажмите «Поездка начата», когда попутчик сядет в машину):",
                    size=14, weight=ft.FontWeight.BOLD),
            accepted_section,
            ft.TextButton("Отменить поездку", icon=ft.Icons.CANCEL, on_click=cancel_trip),
        ]

        render(controls)

    # ---------------- Попутчик: ввод имени -----------------------------
    def ask_passenger_name():
        name_tf = ft.TextField(label="Как вас зовут?", width=300)
        error_text = ft.Text("", color=ft.Colors.RED_600)

        def proceed(e):
            if not name_tf.value or not name_tf.value.strip():
                error_text.value = "Введите имя"
                page.update()
                return
            state["role"] = "passenger"
            state["passenger_name"] = name_tf.value.strip()
            show_passenger_list()

        render([
            ft.Row([
                ft.IconButton(ft.Icons.ARROW_BACK, on_click=lambda e: show_role_selection()),
                ft.Text("Вы — попутчик", size=20, weight=ft.FontWeight.BOLD),
            ]),
            name_tf,
            error_text,
            ft.ElevatedButton("Продолжить", on_click=proceed, width=300),
        ])

    # ---------------- Попутчик: список активных поездок ------------------
    def build_passenger_card(trip):
        full = trip["seats_available"] <= 0

        def accept(e):
            result = store.accept_trip(trip["id"], state["passenger_name"])
            if result is None:
                snack("Извините, места закончились")
                refresh_passenger_cards()
                return
            state["revealed_trip_id"] = trip["id"]
            safe_pubsub_send(PUBSUB_TOPIC)
            show_passenger_contact(result)

        return ft.Card(content=ft.Container(
            padding=16,
            content=ft.Column([
                ft.Text(format_route(trip), size=16, weight=ft.FontWeight.BOLD),
                ft.Text(f"Водитель: {trip['driver_name']}"),
                ft.Text(f"Время: {trip['time']}"),
                ft.Text(f"Цена: {trip['price']} ₽"),
                ft.Text(f"Машина: {trip['car_plate']}"),
                ft.Text(f"Свободно мест: {trip['seats_available']} из {trip['seats_total']}"),
                *([ft.Text(f"Комментарий: {trip['comment']}")] if trip.get("comment") else []),
                ft.ElevatedButton(
                    "Мест нет" if full else "Принять поездку",
                    disabled=full,
                    icon=ft.Icons.CHECK_CIRCLE,
                    on_click=accept,
                ),
            ]),
        ))

    def refresh_passenger_cards():
        """Точечно обновляет только карточки поездок, не трогая шапку
        экрана и не сбрасывая позицию скролла попутчика."""
        list_view = state["passenger_list_view"]
        if list_view is None:
            return

        trips = store.get_active_trips()
        list_view.controls.clear()
        if not trips:
            list_view.controls.append(ft.Text("Пока нет доступных поездок", color=ft.Colors.GREY_600))
        else:
            list_view.controls.extend(build_passenger_card(trip) for trip in trips)
        list_view.update()

    def show_passenger_list():
        state["role"] = "passenger"
        state["revealed_trip_id"] = None

        list_view = ft.ListView(spacing=10, expand=True, auto_scroll=False)
        state["passenger_list_view"] = list_view

        render([
            ft.Row([
                ft.IconButton(ft.Icons.ARROW_BACK, on_click=lambda e: show_role_selection()),
                ft.Text("Доступные поездки", size=20, weight=ft.FontWeight.BOLD),
                ft.IconButton(ft.Icons.REFRESH, on_click=lambda e: refresh_passenger_cards()),
            ]),
            list_view,
        ])
        refresh_passenger_cards()

    # ---------------- Попутчик: контакт после принятия поездки -----------
    def show_passenger_contact(trip):
        # если водитель уже завершил/отменил поездку, пока попутчик смотрел контакт
        fresh = store.get_trip(trip["id"])
        if fresh is None:
            show_role_selection("Поездка была завершена или отменена водителем")
            return

        render([
            ft.Text("Поездка принята!", size=20, weight=ft.FontWeight.BOLD, color=ft.Colors.GREEN_700),
            ft.Card(content=ft.Container(
                padding=16,
                content=ft.Column([
                    ft.Text(format_route(trip), size=16),
                    ft.Text(f"Водитель: {trip['driver_name']}"),
                    ft.Text(f"Время: {trip['time']}"),
                    ft.Text(f"Цена: {trip['price']} ₽"),
                    ft.Text(f"Машина: {trip['car_plate']}"),
                    *([ft.Text(f"Комментарий: {trip['comment']}")] if trip.get("comment") else []),
                    ft.Divider(),
                    ft.Text("Контакт водителя:", weight=ft.FontWeight.BOLD),
                    ft.Text(trip["contact"], size=18, selectable=True),
                ]),
            )),
            ft.Text("Свяжитесь с водителем, чтобы договориться о месте посадки.",
                    color=ft.Colors.GREY_600),
            ft.ElevatedButton("К списку поездок", on_click=lambda e: show_passenger_list(), width=300),
        ])

    # ---------------- Синхронизация между всеми подключёнными клиентами --
    def do_refresh():
        """Общая логика обновления текущего экрана. Вызывается и мгновенно
        по пуш-уведомлению (онлайн, через pubsub), и периодически по таймеру
        (офлайн, локальный опрос без сети) — поэтому вручную нажимать
        «Обновить» больше не нужно ни в одном из режимов."""
        if state["role"] == "driver" and state["trip_id"]:
            show_driver_waiting()
        elif state["role"] == "passenger":
            if state["revealed_trip_id"]:
                fresh = store.get_trip(state["revealed_trip_id"])
                if fresh is None:
                    show_role_selection("Поездка была завершена или отменена водителем")
                # если поездка ещё жива — просто оставляем экран контакта как есть
            elif state["passenger_list_view"] is not None:
                # экран списка уже открыт — обновляем только карточки,
                # без полной перерисовки (шапка и скролл остаются на месте)
                refresh_passenger_cards()

    def on_pubsub(message):
        # мгновенное пуш-уведомление актуально только в онлайн-режиме;
        # в офлайне экран и так обновляется сам по таймеру (см. ниже)
        if state["online_mode"]:
            do_refresh()

    try:
        page.pubsub.subscribe(on_pubsub)
    except RuntimeError:
        pass

    # ---------------- Локальный автоопрос для офлайн-режима --------------
    # Пока переключатель стоит в положении «Офлайн», сетевые
    # пуш-уведомления не используются, поэтому раз в пару секунд сами
    # проверяем store и обновляем экран — без всякого ручного нажатия.
    stop_polling = threading.Event()

    async def polling_loop():
        while True:
            await asyncio.sleep(2)
            if not state["online_mode"]:
                try:
                    do_refresh()
                except Exception:
                    # сессия могла закрыться между проверками — просто выходим
                    return

    # Запускаем фоновую задачу через page.run_task (это asyncio, а не OS-поток)
    page.run_task(polling_loop)

    show_role_selection()


if __name__ == "__main__":
    # view=ft.AppView.WEB_BROWSER — запускает локальный веб-сервер иcd
    # открывает интерфейс в браузере по http://127.0.0.1:8550.
    # Несколько человек в одной сети могут одновременно открыть этот
    # адрес каждый со своего устройства (водитель и попутчики).
    ft.app(target=main)  # , view=ft.AppView.WEB_BROWSER, port=8550)
