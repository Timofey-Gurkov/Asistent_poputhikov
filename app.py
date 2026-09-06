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
get_trip_by_code) и подставить его вместо TripStore — код экранов
трогать не придётся.

В шапке приложения есть переключатель «Онлайн» / «Офлайн»:
  • Онлайн (по умолчанию) — экраны водителя и попутчика обновляются
    сами по себе, как только в сети появляются новые поездки/отклики.
  • Офлайн — автоматические уведомления сети игнорируются, экран
    обновляется только вручную кнопкой «Обновить» (иконка со стрелками).
    Это позволяет пользоваться приложением даже при нестабильном
    подключении, не тратя ресурсы на постоянные фоновые обновления.
"""

import random
import threading
import uuid

import flet as ft

# --------------------------------------------------------------------------
# 1. Слой хранения данных (легко заменить на BLE в будущем)
# --------------------------------------------------------------------------

DIRECTIONS = ["ГУК УрФУ → НВК", "НВК → ГУК УрФУ"]


class TripStore:
    """Хранит все активные поездки в обычном списке в оперативной памяти.

    Никакой базы данных нет: при перезапуске приложения все поездки
    пропадают, что полностью соответствует требованию "без БД".
    """

    def __init__(self):
        self._trips = [] # список словарей-поездок
        self._lock = threading.Lock()

    # ---- запись -----------------------------------------------------
    def add_trip(self, trip: dict) -> None:
        with self._lock:
            self._trips.append(trip)

    def accept_trip(self, code: str, passenger_name: str):
        """Попутчик принимает поездку. Возвращает поездку либо None,
        если мест не осталось или код не найден.

        Каждый принявший попутчик получает свой уникальный id — по нему
        водитель потом сможет начать поездку именно для этого человека,
        даже если у нескольких попутчиков совпадают имена."""
        with self._lock:
            for trip in self._trips:
                if trip["code"] == code and trip["seats_available"] > 0:
                    trip["seats_available"] -= 1
                    trip["accepted"].append({
                        "id": uuid.uuid4().hex[:8],
                        "name": passenger_name,
                    })
                    return dict(trip)
        return None

    def start_passenger(self, code: str, passenger_id: str) -> bool:
        """Водитель начинает поездку для конкретного попутчика: тот
        убирается из списка ожидающих посадки. Если это был последний
        ожидающий попутчик, вся поездка считается начатой и её данные
        удаляются полностью. Возвращает True, если поездка была удалена."""
        with self._lock:
            for trip in self._trips:
                if trip["code"] == code:
                    trip["accepted"] = [
                        p for p in trip["accepted"] if p["id"] != passenger_id
                    ]
                    if not trip["accepted"]:
                        self._trips.remove(trip)
                        return True
                    return False
        return False

    def finish_trip(self, code: str) -> None:
        """Полностью удаляет все данные о поездке (отмена поездки
        водителем)."""
        with self._lock:
            self._trips[:] = [t for t in self._trips if t["code"] != code]

    # ---- чтение -------------------------------------------------------
    def get_active_trips(self):
        with self._lock:
            return [dict(t) for t in self._trips]

    def get_trip_by_code(self, code: str):
        with self._lock:
            for trip in self._trips:
                if trip["code"] == code:
                    return dict(trip)
        return None

    def existing_codes(self):
        with self._lock:
            return {t["code"] for t in self._trips}


store = TripStore()
PUBSUB_TOPIC = "trips_updated"


def generate_code(existing: set) -> str:
    """Генерирует уникальный 3-значный код поездки (000-999)."""
    while True:
        code = f"{random.randint(0, 999):03d}"
        if code not in existing:
            return code


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
        "role": None, # "driver" | "passenger"
        "trip_code": None, # код поездки, созданной этим водителем
        "passenger_name": None,
        "revealed_code": None, # код поездки, чей контакт уже раскрыт попутчику
        "passenger_list_view": None, # активный ListView экрана списка поездок (если открыт)
        "online_mode": True, # вкл — автообновление по сети; выкл — только вручную (офлайн)
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
            "Автообновление включено: новые поездки и отклики будут появляться сами"
            if state["online_mode"]
            else "Офлайн-режим: обновляйте список кнопкой «Обновить»"
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

    # ---------------- Экран выбора роли -------------------------------
    def show_role_selection(message: str | None = None):
        state["role"] = None
        state["trip_code"] = None
        state["revealed_code"] = None
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

        direction_dd = ft.Dropdown(
            label="Направление",
            width=300,
            options=[ft.dropdown.Option(d) for d in DIRECTIONS],
            value=DIRECTIONS[0],
        )
        seats_tf = ft.TextField(label="Количество мест", width=300, keyboard_type=ft.KeyboardType.NUMBER)
        time_tf = ft.TextField(label="Время отправления (напр. 18:30)", width=300)
        price_tf = ft.TextField(label="Цена, ₽", width=300, keyboard_type=ft.KeyboardType.NUMBER)
        contact_tf = ft.TextField(label="Контакт (телефон / телеграм)", width=300)
        error_text = ft.Text("", color=ft.Colors.RED_600)

        def create_trip(e):
            try:
                seats = int(seats_tf.value)
                if seats <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                error_text.value = "Укажите корректное число мест"
                page.update()
                return

            if not time_tf.value or not price_tf.value.strip() or not contact_tf.value.strip():
                error_text.value = "Заполните время, цену и контакт"
                page.update()
                return

            code = generate_code(store.existing_codes())
            trip = {
                "code": code,
                "direction": direction_dd.value,
                "seats_total": seats,
                "seats_available": seats,
                "time": time_tf.value.strip(),
                "price": price_tf.value.strip(),
                "contact": contact_tf.value.strip(),
                "accepted": [],
            }
            store.add_trip(trip)
            state["trip_code"] = code
            page.pubsub.send_all(PUBSUB_TOPIC)
            show_driver_waiting()

        render([
            ft.Row([
                ft.IconButton(ft.Icons.ARROW_BACK, on_click=lambda e: show_role_selection()),
                ft.Text("Создание поездки", size=20, weight=ft.FontWeight.BOLD),
            ]),
            direction_dd,
            seats_tf,
            time_tf,
            price_tf,
            contact_tf,
            error_text,
            ft.ElevatedButton("Опубликовать поездку", icon=ft.Icons.CHECK, on_click=create_trip, width=300),
        ])

    # ---------------- Водитель: экран ожидания попутчиков ---------------
    def show_driver_waiting():
        trip = store.get_trip_by_code(state["trip_code"])
        if trip is None:
            show_role_selection("Поездка завершена")
            return

        def cancel_trip(e):
            store.finish_trip(trip["code"])
            page.pubsub.send_all(PUBSUB_TOPIC)
            show_role_selection("Поездка отменена")

        def make_start_handler(passenger_id, passenger_name):
            def handler(e):
                trip_removed = store.start_passenger(trip["code"], passenger_id)
                page.pubsub.send_all(PUBSUB_TOPIC)
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
                    ft.Text(f"Код поездки: {trip['code']}", size=24, weight=ft.FontWeight.BOLD),
                    ft.Text(trip["direction"], size=16),
                    ft.Text(f"Время: {trip['time']}"),
                    ft.Text(f"Цена: {trip['price']} ₽"),
                    ft.Text(f"Свободно мест: {trip['seats_available']} из {trip['seats_total']}"),
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
            result = store.accept_trip(trip["code"], state["passenger_name"])
            if result is None:
                snack("Извините, места закончились")
                refresh_passenger_cards()
                return
            state["revealed_code"] = trip["code"]
            page.pubsub.send_all(PUBSUB_TOPIC)
            show_passenger_contact(result)

        return ft.Card(content=ft.Container(
            padding=16,
            content=ft.Column([
                ft.Text(trip["direction"], size=16, weight=ft.FontWeight.BOLD),
                ft.Text(f"Код: {trip['code']}"),
                ft.Text(f"Время: {trip['time']}"),
                ft.Text(f"Цена: {trip['price']} ₽"),
                ft.Text(f"Свободно мест: {trip['seats_available']} из {trip['seats_total']}"),
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
        state["revealed_code"] = None

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
        fresh = store.get_trip_by_code(trip["code"])
        if fresh is None:
            show_role_selection("Поездка была завершена или отменена водителем")
            return

        render([
            ft.Text("Поездка принята!", size=20, weight=ft.FontWeight.BOLD, color=ft.Colors.GREEN_700),
            ft.Card(content=ft.Container(
                padding=16,
                content=ft.Column([
                    ft.Text(trip["direction"], size=16),
                    ft.Text(f"Время: {trip['time']}"),
                    ft.Text(f"Цена: {trip['price']} ₽"),
                    ft.Text(f"Код поездки: {trip['code']}"),
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
    def on_pubsub(message):
        if not state["online_mode"]:
            # офлайн-режим: игнорируем автоматические уведомления сети,
            # пользователь сам обновит экран кнопкой «Обновить»
            return
        if state["role"] == "driver" and state["trip_code"]:
            show_driver_waiting()
        elif state["role"] == "passenger":
            if state["revealed_code"]:
                fresh = store.get_trip_by_code(state["revealed_code"])
                if fresh is None:
                    show_role_selection("Поездка была завершена или отменена водителем")
                # если поездка ещё жива — просто оставляем экран контакта как есть
            elif state["passenger_list_view"] is not None:
                # экран списка уже открыт — обновляем только карточки,
                # без полной перерисовки (шапка и скролл остаются на месте)
                refresh_passenger_cards()

    page.pubsub.subscribe(on_pubsub)

    show_role_selection()


if __name__ == "__main__":
    # view=ft.AppView.WEB_BROWSER — запускает локальный веб-сервер и
    # открывает интерфейс в браузере по http://127.0.0.1:8550.
    # Несколько человек в одной сети могут одновременно открыть этот
    # адрес каждый со своего устройства (водитель и попутчики).
    ft.app(target=main, view=ft.AppView.WEB_BROWSER, port=8550)
