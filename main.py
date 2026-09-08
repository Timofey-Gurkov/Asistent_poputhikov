"""
Приложение поиска попутчиков ГУК УрФУ <-> НВК
"""
import asyncio
import re
import threading
import uuid
import flet as ft


# --------------------------------------------------------------------------
# 1. Слой хранения данных
# --------------------------------------------------------------------------

# Список локаций для формы водителя (пункты «Откуда» / «Куда»).
LOCATIONS = ["ГУК УрФУ", "НВК КПП 1", "НВК КПП 2", "НВК общественный центр"]

# Список локаций для экрана карты.
MAP_LOCATIONS = ["ГУК УрФУ", "НВК"]

# Маппинг: название локации → имя PNG-файла (только латиница!).
MAP_FILES = {
    "ГУК УрФУ": "guk_urfu.png",
    "НВК": "nvk.png",
}

# ---- Валидация полей формы водителя --------------------------------------

TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
PHONE_RE = re.compile(r"^(\+7|8|7)\d{10}$")
TELEGRAM_RE = re.compile(r"^@[A-Za-z0-9_]{5,32}$")


def validate_time(value: str) -> bool:
    return bool(TIME_RE.match((value or "").strip()))


def validate_price(value: str) -> bool:
    """Цена — неотрицательное число. Ноль разрешён."""
    text = (value or "").strip().replace(",", ".")
    try:
        price = float(text)
    except ValueError:
        return False
    return price >= 0


def validate_contact(value: str) -> bool:
    text = (value or "").strip()
    digits_only = re.sub(r"[\s\-()]", "", text)
    return bool(PHONE_RE.match(digits_only) or TELEGRAM_RE.match(text))


PLATE_RE = re.compile(r"^[АВЕКМНОРСТУХ]\d{3}[АВЕКМНОРСТУХ]{2}\d{2,3}$")


def normalize_plate(value: str) -> str:
    return (value or "").strip().upper().replace(" ", "").replace("Ё", "Е")


def validate_plate(value: str) -> bool:
    return bool(PLATE_RE.match(normalize_plate(value)))


def format_route(trip: dict) -> str:
    return f"{trip['from']} → {trip['to']}"


class TripStore:
    def __init__(self):
        self._trips = []
        self._lock = threading.Lock()

    def add_trip(self, trip: dict) -> None:
        with self._lock:
            self._trips.append(trip)

    def accept_trip(self, trip_id: str, passenger_name: str):
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

    def reject_trip(self, trip_id: str, passenger_name: str) -> bool:
        """Попутчик отказывается от поездки. Возвращает место водителю."""
        with self._lock:
            for trip in self._trips:
                if trip["id"] == trip_id:
                    for i, p in enumerate(trip["accepted"]):
                        if p["name"] == passenger_name:
                            trip["accepted"].pop(i)
                            trip["seats_available"] += 1
                            return True
        return False

    def start_passenger(self, trip_id: str, passenger_id: str) -> bool:
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
        with self._lock:
            self._trips[:] = [t for t in self._trips if t["id"] != trip_id]

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
    page.title = "Кампус-экспресс"
    page.window_width = 420
    page.window_height = 780
    page.horizontal_alignment = ft.CrossAxisAlignment.CENTER
    page.scroll = ft.ScrollMode.AUTO
    page.padding = 20

    state = {
        "role": None,
        "trip_id": None,
        "passenger_name": None,
        "revealed_trip_id": None,
        "passenger_list_view": None,
        "online_mode": True,
        "last_screen_func": None,
    }

    content = ft.Column(spacing=16, width=380)

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

    online_switch = ft.Switch(value=True, on_change=toggle_online, scale=0.85)
    header = ft.Row(
        [
            ft.Text("Кампус-экспресс", size=16, weight=ft.FontWeight.BOLD),
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
        try:
            page.pubsub.send_all(topic)
        except RuntimeError:
            pass

    def map_src(location_name: str) -> str:
        """Формирует путь к файлу карты."""
        filename = MAP_FILES.get(location_name, "guk_urfu.png")
        return f"maps/{filename}"

    # ---------------- Экран карты ---------------------------------------
    def show_map_screen():
        if state["role"] == "driver" and state["trip_id"]:
            state["last_screen_func"] = lambda: show_driver_waiting()
        elif state["role"] == "passenger":
            if state["revealed_trip_id"]:
                fresh = store.get_trip(state["revealed_trip_id"])
                if fresh:
                    state["last_screen_func"] = lambda: show_passenger_contact(fresh)
                else:
                    state["last_screen_func"] = lambda: show_role_selection()
            elif state["passenger_list_view"] is not None:
                state["last_screen_func"] = lambda: show_passenger_list()
            else:
                state["last_screen_func"] = lambda: show_role_selection()
        else:
            state["last_screen_func"] = lambda: show_role_selection()

        map_image = ft.Image(
            src=map_src(MAP_LOCATIONS[0]),
        )

        location_dropdown = ft.Dropdown(
            label="Выберите локацию",
            width=300,
            options=[ft.dropdown.Option(loc) for loc in MAP_LOCATIONS],
            value=MAP_LOCATIONS[0],
        )

        last_selected = {"value": MAP_LOCATIONS[0]}

        async def dropdown_polling():
            while True:
                await asyncio.sleep(0.5)
                if not location_dropdown.page:
                    return
                current = location_dropdown.value
                if current and current != last_selected["value"]:
                    last_selected["value"] = current
                    map_image.src = map_src(current)
                    try:
                        map_image.update()
                    except Exception:
                        return

        page.run_task(dropdown_polling)

        def go_back(e):
            if state["last_screen_func"]:
                state["last_screen_func"]()
            else:
                show_role_selection()

        render([
            ft.Row([
                ft.IconButton(ft.Icons.ARROW_BACK, on_click=go_back),
                ft.Text("Карта", size=20, weight=ft.FontWeight.BOLD),
            ]),
            location_dropdown,
            map_image,
        ])

    # ---------------- Экран выбора роли -------------------------------
    def show_role_selection(message: str | None = None):
        state["role"] = None
        state["trip_id"] = None
        state["revealed_trip_id"] = None
        state["passenger_list_view"] = None
        state["last_screen_func"] = None
        controls = [
            ft.Text("Найдем попутчиков рядом", size=26, weight=ft.FontWeight.BOLD),
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

        from_dd = ft.Dropdown(
            label="Откуда",
            width=300,
            options=[ft.dropdown.Option(loc) for loc in LOCATIONS],
            value=LOCATIONS[0],
        )
        to_dd = ft.Dropdown(
            label="Куда",
            width=300,
            options=[ft.dropdown.Option(loc) for loc in LOCATIONS],
            value=LOCATIONS[1] if len(LOCATIONS) > 1 else LOCATIONS[0],
        )

        seats_dd = ft.Dropdown(
            label="Количество мест",
            width=300,
            options=[ft.dropdown.Option(str(n)) for n in range(1, 9)],
            value="1",
        )
        time_tf = ft.TextField(label="Время отправления, например 18:30", width=300)
        price_tf = ft.TextField(
            label="Цена, ₽",
            width=300,
            value="0",
            keyboard_type=ft.KeyboardType.NUMBER,
        )
        contact_tf = ft.TextField(
            label="Контакт: телефон +7... или телеграм @username", width=300
        )
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
            if not from_dd.value:
                error_text.value = "Выберите пункт «Откуда»"
                page.update()
                return
            if not to_dd.value:
                error_text.value = "Выберите пункт «Куда»"
                page.update()
                return
            if from_dd.value.strip().lower() == to_dd.value.strip().lower():
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
                error_text.value = "Цена должна быть неотрицательным числом"
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
                "from": from_dd.value.strip(),
                "to": to_dd.value.strip(),
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
            from_dd,
            to_dd,
            seats_dd,
            time_tf,
            price_tf,
            contact_tf,
            plate_tf,
            comment_tf,
            error_text,
            ft.ElevatedButton(
                "Опубликовать поездку",
                icon=ft.Icons.CHECK,
                on_click=create_trip,
                width=300,
            ),
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
                            on_click=make_start_handler(
                                passenger["id"], passenger["name"]
                            ),
                        ),
                    ],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                )
                for passenger in trip["accepted"]
            ]
            accepted_section = ft.Column(accepted_rows, spacing=10)
        else:
            accepted_section = ft.Text(
                "Пока никто не откликнулся", color=ft.Colors.GREY_600
            )

        controls = [
            ft.Row([
                ft.IconButton(ft.Icons.ARROW_BACK, on_click=lambda e: cancel_trip(e)),
                ft.Text("Ваша поездка", size=20, weight=ft.FontWeight.BOLD),
                ft.IconButton(ft.Icons.REFRESH, on_click=lambda e: show_driver_waiting()),
            ]),
            ft.ElevatedButton(
                "Карта",
                icon=ft.Icons.MAP,
                width=300,
                on_click=lambda e: show_map_screen(),
            ),
            ft.Card(content=ft.Container(
                padding=16,
                content=ft.Column([
                    ft.Text(
                        f"Водитель: {trip['driver_name']}",
                        size=18, weight=ft.FontWeight.BOLD,
                    ),
                    ft.Text(format_route(trip), size=16),
                    ft.Text(f"Время: {trip['time']}"),
                    ft.Text(f"Цена: {trip['price']} ₽"),
                    ft.Text(f"Машина: {trip['car_plate']}"),
                    ft.Text(
                        f"Свободно мест: {trip['seats_available']} "
                        f"из {trip['seats_total']}"
                    ),
                    *([ft.Text(f"Комментарий: {trip['comment']}")]
                      if trip.get("comment") else []),
                ]),
            )),
            ft.Text(
                "Отклики (нажмите «Поездка начата», когда попутчик сядет в машину):",
                size=14, weight=ft.FontWeight.BOLD,
            ),
            accepted_section,
            ft.TextButton(
                "Отменить поездку", icon=ft.Icons.CANCEL, on_click=cancel_trip
            ),
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
                ft.Text(
                    f"Свободно мест: {trip['seats_available']} "
                    f"из {trip['seats_total']}"
                ),
                *([ft.Text(f"Комментарий: {trip['comment']}")]
                  if trip.get("comment") else []),
                ft.ElevatedButton(
                    "Мест нет" if full else "Принять поездку",
                    disabled=full,
                    icon=ft.Icons.CHECK_CIRCLE,
                    on_click=accept,
                ),
            ]),
        ))

    def refresh_passenger_cards():
        list_view = state["passenger_list_view"]
        if list_view is None:
            return
        trips = store.get_active_trips()
        list_view.controls.clear()
        if not trips:
            list_view.controls.append(
                ft.Text("Пока нет доступных поездок", color=ft.Colors.GREY_600)
            )
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
            ft.ElevatedButton(
                "Карта",
                icon=ft.Icons.MAP,
                width=300,
                on_click=lambda e: show_map_screen(),
            ),
            list_view,
        ])
        refresh_passenger_cards()

    # ---------------- Попутчик: контакт после принятия поездки -----------
    def show_passenger_contact(trip):
        fresh = store.get_trip(trip["id"])
        if fresh is None:
            show_role_selection("Поездка была завершена или отменена водителем")
            return

        def reject_trip_handler(e):
            store.reject_trip(trip["id"], state["passenger_name"])
            safe_pubsub_send(PUBSUB_TOPIC)
            state["revealed_trip_id"] = None
            show_passenger_list()

        render([
            ft.Text(
                "Поездка принята!", size=20,
                weight=ft.FontWeight.BOLD, color=ft.Colors.GREEN_700,
            ),
            ft.Card(content=ft.Container(
                padding=16,
                content=ft.Column([
                    ft.Text(format_route(trip), size=16),
                    ft.Text(f"Водитель: {trip['driver_name']}"),
                    ft.Text(f"Время: {trip['time']}"),
                    ft.Text(f"Цена: {trip['price']} ₽"),
                    ft.Text(f"Машина: {trip['car_plate']}"),
                    *([ft.Text(f"Комментарий: {trip['comment']}")]
                      if trip.get("comment") else []),
                    ft.Divider(),
                    ft.Text("Контакт водителя:", weight=ft.FontWeight.BOLD),
                    ft.Text(trip["contact"], size=18, selectable=True),
                ]),
            )),
            ft.Text(
                "Свяжитесь с водителем, чтобы договориться о месте посадки.",
                color=ft.Colors.GREY_600,
            ),
            ft.ElevatedButton(
                "Карта",
                icon=ft.Icons.MAP,
                width=300,
                on_click=lambda e: show_map_screen(),
            ),
            ft.ElevatedButton(
                "Отказаться от поездки",
                icon=ft.Icons.CLOSE,
                on_click=reject_trip_handler,
                width=300,
            ),
        ])

    # ---------------- Синхронизация между клиентами ---------------------
    def do_refresh():
        if state["role"] == "driver" and state["trip_id"]:
            show_driver_waiting()
        elif state["role"] == "passenger":
            if state["revealed_trip_id"]:
                fresh = store.get_trip(state["revealed_trip_id"])
                if fresh is None:
                    show_role_selection(
                        "Поездка была завершена или отменена водителем"
                    )
            elif state["passenger_list_view"] is not None:
                refresh_passenger_cards()

    def on_pubsub(message):
        if state["online_mode"]:
            do_refresh()

    try:
        page.pubsub.subscribe(on_pubsub)
    except RuntimeError:
        pass

    async def polling_loop():
        while True:
            await asyncio.sleep(2)
            if not state["online_mode"]:
                try:
                    do_refresh()
                except Exception:
                    return

    page.run_task(polling_loop)
    show_role_selection()


if __name__ == "__main__":
    ft.app(
        target=main,
        view=ft.AppView.WEB_BROWSER,
        port=8550,
        assets_dir="assets",
    )