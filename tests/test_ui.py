import asyncio

from core.models import PrinterProfile
from core.threemf import ObjectLabelMatch, PlateObject
from ui.__main__ import (
    _bed_rect,
    _bed_canvas_shapes,
    _available_object_layers,
    _can_skip_selected_labels,
    _bed_view_size,
    _effective_theme,
    _format_remaining_time,
    _job_file,
    _print_control_availability,
    _reported_skip_label_ids,
    app_main,
)


class FakePage:
    def __init__(self):
        self.added = []
        self.dialogs = []
        self.on_close = None
        self.on_resize = None
        self.on_platform_brightness_change = None
        self.platform_brightness = None
        self.width = 1200
        self.scroll = None
        self.tasks = []
        self.update_count = 0

    def add(self, *controls):
        self.added.extend(controls)

    def update(self):
        self.update_count += 1

    def run_task(self, callback):
        self.tasks.append(callback)

    def show_dialog(self, dialog):
        self.dialogs.append(dialog)

    def pop_dialog(self):
        pass


def _find_control(control, predicate):
    if predicate(control):
        return control
    children = list(getattr(control, "controls", []) or [])
    content = getattr(control, "content", None)
    if content is not None and not isinstance(content, str):
        children.append(content)
    for child in children:
        found = _find_control(child, predicate)
        if found is not None:
            return found
    return None


def test_job_filename_uses_safe_basename():
    assert _job_file({"gcode_file": "/cache/Куб.gcode.3mf"}) == "Куб.gcode.3mf"
    assert _job_file({"gcode_file": r"C:\cache\Куб.gcode.3mf"}) == "Куб.gcode.3mf"
    assert _job_file({"gcode_file": "not-a-3mf.gcode"}) is None


def test_theme_mode_respects_manual_and_system_brightness():
    import flet as ft

    assert _effective_theme(ft.ThemeMode.LIGHT, ft.Brightness.DARK) == "light"
    assert _effective_theme(ft.ThemeMode.DARK, ft.Brightness.LIGHT) == "dark"
    assert _effective_theme(ft.ThemeMode.SYSTEM, ft.Brightness.DARK) == "dark"
    assert _effective_theme(ft.ThemeMode.SYSTEM, ft.Brightness.LIGHT) == "light"
    assert _effective_theme(ft.ThemeMode.SYSTEM, None) == "light"


def test_bed_view_size_adapts_to_window_width():
    assert _bed_view_size(None) == 460
    assert _bed_view_size(390) == 298
    assert _bed_view_size(1200) == 460
    assert _bed_view_size(200) == 240


def test_print_control_availability_tracks_printer_state():
    assert _print_control_availability("RUNNING", True, False) == (True, False, True)
    assert _print_control_availability("PAUSE", True, False) == (False, True, True)
    assert _print_control_availability("PREPARE", True, False) == (False, False, True)
    assert _print_control_availability("IDLE", True, False) == (False, False, False)
    assert _print_control_availability("RUNNING", False, False) == (False, False, False)
    assert _print_control_availability("RUNNING", True, True) == (False, False, False)


def test_printer_state_label_and_finish_estimate():
    from ui.__main__ import _estimated_finish_time, _printer_state_label

    assert _printer_state_label("RUNNING") == "Друкує"
    assert _printer_state_label("PAUSE") == "На паузі"
    assert _printer_state_label("IDLE") == "Готовий"
    assert _printer_state_label(None) == "Стан невідомий"
    estimate = _estimated_finish_time(0)
    assert estimate.startswith("Завершення ~")
    assert len(estimate) == len("Завершення ~00:00")
    assert _estimated_finish_time(None) == "Завершення: —"


def test_remaining_time_formats_minutes_as_hours_and_minutes():
    assert _format_remaining_time(21) == "21 хв"
    assert _format_remaining_time(60) == "1 год"
    assert _format_remaining_time(75) == "1 год 15 хв"
    assert _format_remaining_time(None) == "—"


def test_reported_skip_label_ids_normalize_report_values():
    assert _reported_skip_label_ids(["198", {"id": 163}, {"label_id": "124"}, "invalid"]) == {
        124,
        163,
        198,
    }


def test_skip_selection_requires_valid_ids_and_keeps_one_object_printing():
    matches = [
        ObjectLabelMatch(1, object_id, label_id, 3, 3, 1.0)
        for object_id, label_id in ((116, 87), (272, 122), (307, 278))
    ]

    assert not _can_skip_selected_labels(set(), matches, set())
    assert _can_skip_selected_labels({87}, matches, set())
    assert _can_skip_selected_labels({87, 122}, matches, set())
    assert not _can_skip_selected_labels({87, 122, 278}, matches, set())
    assert not _can_skip_selected_labels({999}, matches, set())
    assert _can_skip_selected_labels({122}, matches, {87})
    assert not _can_skip_selected_labels({122, 278}, matches, {87})
    assert not _can_skip_selected_labels({87}, matches, {87})


def test_filled_button_selection_count_uses_flet_content_property():
    import flet as ft

    button = ft.FilledButton(content="Пропустити вибрані (0)")
    button.content = "Пропустити вибрані (2)"

    assert button.content == "Пропустити вибрані (2)"


def test_object_skip_capability_uses_firmware_fun_bit_49():
    from core.limits import object_skip_flag

    assert object_skip_flag("2000000000000") is True
    assert object_skip_flag("0x2000000000000") is True
    assert object_skip_flag("0") is False
    assert object_skip_flag(None) is None
    assert object_skip_flag("invalid") is None


def test_bed_rect_maps_coordinates_to_top_down_view():
    obj = PlateObject(1, 83, "part", (18.0, 36.0, 54.0, 72.0))
    assert _bed_rect(obj, bed_size_mm=180.0, view_size=180.0) == (
        18.0,
        108.0,
        36.0,
        36.0,
    )


def test_bed_canvas_draws_first_layer_paths_in_bed_coordinates():
    obj = PlateObject(1, 83, "part", (10.0, 20.0, 30.0, 40.0))
    match = ObjectLabelMatch(1, 83, 198, 2, 2, 1.0)
    paths = {(1, 198): ((((10.0, 20.0), (30.0, 40.0)),),)}

    shapes = _bed_canvas_shapes([obj], [match], paths, 460.0, set())

    assert len(shapes) == 3
    path = shapes[2]
    assert path.elements[0].x == 10.0
    assert path.elements[0].y == 440.0
    assert path.elements[1].x == 30.0
    assert path.elements[1].y == 420.0
    assert path.paint.color == "#0F766E"


def test_bed_canvas_scales_for_narrow_view_and_dark_theme():
    obj = PlateObject(1, 83, "part", (10.0, 20.0, 30.0, 40.0))
    match = ObjectLabelMatch(1, 83, 198, 2, 2, 1.0)
    paths = {(1, 198): ((((10.0, 20.0), (30.0, 40.0)),),)}

    shapes = _bed_canvas_shapes(
        [obj],
        [match],
        paths,
        180.0,
        set(),
        view_size=320.0,
        theme="dark",
    )

    assert shapes[0].width == 320
    assert shapes[0].height == 320
    assert shapes[0].paint.color == "#111827"
    assert shapes[2].elements[0].x == 320 / 180 * 10


def test_bed_canvas_overlays_selected_layer_with_per_object_shades():
    obj = PlateObject(1, 83, "part", (10.0, 20.0, 30.0, 40.0))
    match = ObjectLabelMatch(1, 83, 198, 2, 2, 1.0)
    paths = {
        (1, 198): (
            (((10.0, 20.0), (20.0, 30.0)),),
            (((20.0, 30.0), (30.0, 40.0)),),
        ),
    }

    overlay = _bed_canvas_shapes(
        [obj], [match], paths, 460.0, set(), selected_layer=2, overlay_layers=True
    )
    selected = _bed_canvas_shapes(
        [obj], [match], paths, 460.0, set(), selected_layer=2, overlay_layers=False
    )

    assert len(overlay) == 4
    assert overlay[2].paint.color == "#CCFBF1"
    assert overlay[3].paint.color == "#99F6E4"
    assert len(selected) == 3
    assert selected[2].paint.color == "#0F766E"


def test_available_object_layers_ignores_empty_layer_markers():
    obj = PlateObject(1, 83, "part", (10.0, 20.0, 30.0, 40.0))
    match = ObjectLabelMatch(1, 83, 198, 2, 2, 1.0)
    paths = {
        (1, 198): (
            (((10.0, 20.0), (15.0, 25.0)),),
            (),
            (((15.0, 25.0), (20.0, 30.0)),),
        ),
    }

    assert _available_object_layers([obj], [match], paths) == 1
    assert _available_object_layers([obj], [match], {(1, 198): ((), (), ())}) == 0


def test_flet_page_builds_without_connecting_to_printer():
    import flet as ft

    page = FakePage()

    asyncio.run(app_main(page))

    assert len(page.added) == 1
    assert len(page.added[0].controls) == 6
    assert page.update_count == 1
    assert len(page.tasks) == 2
    assert page.on_close is not None
    assert page.scroll is None
    assert page.added[0].scroll == ft.ScrollMode.AUTO
    header = page.added[0].controls[0]
    assert header.controls == [
        _find_control(page.added[0], lambda control: isinstance(control, ft.Text)
                      and control.value == "BambuDeck"),
        _find_control(page.added[0], lambda control: isinstance(control, ft.Dropdown)
                      and control.label == "Тема"),
    ]
    assert header.alignment == ft.MainAxisAlignment.SPACE_BETWEEN
    printer_cards = page.added[0].controls[3]
    assert isinstance(printer_cards, ft.Column)
    assert printer_cards.tight is True
    assert printer_cards.controls
    card = printer_cards.controls[0]
    assert isinstance(card, ft.Container)
    assert card.ink is False
    assert card.content.tight is True
    nested_columns = [
        control
        for row in (card.content.controls[0], card.content.controls[2])
        for control in row.controls
        if isinstance(control, ft.Column)
    ]
    assert all(column.expand is None for column in nested_columns)
    asyncio.run(card.on_click(None))
    detail_panel = page.added[0].controls[5]
    assert detail_panel.visible is True
    bed_card = _find_control(
        detail_panel,
        lambda control: isinstance(control, ft.Container)
        and isinstance(control.content, ft.Column)
        and any(
            isinstance(child, ft.Text) and child.value == "Площина столу"
            for child in control.content.controls
        ),
    )
    assert bed_card is not None
    assert bed_card.visible is False
    objects_row = _find_control(detail_panel, lambda control: isinstance(control, ft.ResponsiveRow))
    assert objects_row is not None
    assert objects_row.visible is False
    bed_stack = _find_control(detail_panel, lambda control: isinstance(control, ft.Stack))
    assert bed_stack is not None
    assert bed_stack.controls == []
    collapse_button = _find_control(
        detail_panel,
        lambda control: isinstance(control, ft.TextButton)
        and control.content == "Згорнути керування",
    )
    asyncio.run(collapse_button.on_click(None))
    assert detail_panel.visible is False
    asyncio.run(card.on_click(None))
    assert detail_panel.visible is True
    for label in ("Пауза", "Продовжити", "Скасувати друк"):
        button = _find_control(
            page.added[0],
            lambda control: isinstance(control, ft.FilledButton)
            and control.content == label,
        )
        assert button is not None
        assert button.visible is False
        assert button.disabled is True


def test_add_printer_dialog_explains_dhcp_reservation_and_masks_access_code():
    import flet as ft

    page = FakePage()
    asyncio.run(app_main(page))
    add_button = _find_control(
        page.added[0],
        lambda control: isinstance(control, ft.FilledButton)
        and control.content == "Додати принтер",
    )

    asyncio.run(add_button.on_click(None))

    assert len(page.dialogs) == 1
    dialog = page.dialogs[0]
    fields = dialog.content.controls
    assert isinstance(fields[1], ft.TextField)
    assert fields[1].label == "Модель (необов'язково)"
    assert any(
        isinstance(control, ft.Text) and "DHCP роутера" in control.value
        for control in fields
    )
    access_code = next(
        control
        for control in fields
        if isinstance(control, ft.TextField) and control.password
    )
    assert access_code.label.startswith("Код доступу LAN")


def test_printer_card_connect_shows_live_job_summary(monkeypatch):
    import flet as ft
    import ui.__main__ as ui_module

    class FakePrinterClient:
        def __init__(self, profile):
            self.profile = profile

        def connect(self):
            pass

        def wait_for_state(self, timeout):
            return True

        def snapshot(self):
            return {
                "print": {
                    "gcode_state": "RUNNING",
                    "mc_percent": 54,
                    "mc_remaining_time": 20,
                    "subtask_name": "Test print",
                }
            }

        @property
        def online(self):
            return True

        def close(self):
            pass

    monkeypatch.setattr(ui_module, "PrinterClient", FakePrinterClient)
    monkeypatch.setattr(
        ui_module,
        "load_profiles",
        lambda: [
            PrinterProfile(
                name="Test printer",
                ip="192.168.1.50",
                serial="TEST-SERIAL",
                access_code="12345678",
                enabled=True,
                id="test-printer",
            )
        ],
    )
    page = FakePage()
    asyncio.run(app_main(page))
    printer_cards = page.added[0].controls[3]
    asyncio.run(printer_cards.controls[0].on_click(None))
    card = page.added[0].controls[3].controls[0]
    connect_button = _find_control(
        card,
        lambda control: isinstance(control, ft.OutlinedButton)
        and control.content == "Підключити",
    )

    asyncio.run(connect_button.on_click(None))

    updated_card = page.added[0].controls[3].controls[0]
    card_texts = []

    def collect_text(control):
        if isinstance(control, ft.Text):
            card_texts.append(control.value)
        for child in getattr(control, "controls", []) or []:
            collect_text(child)
        child = getattr(control, "content", None)
        if child is not None and not isinstance(child, str):
            collect_text(child)

    collect_text(updated_card)
    assert "Друкує" in card_texts
    assert "Test print" in card_texts
    assert any(text.startswith("54% · 20 хв до завершення") for text in card_texts)
    assert any("Завершення ~" in text for text in card_texts)


def test_flet_theme_selector_updates_theme_and_page_colors():
    import flet as ft

    page = FakePage()
    asyncio.run(app_main(page))
    theme_dropdown = _find_control(
        page.added[0],
        lambda control: isinstance(control, ft.Dropdown)
        and control.label == "Тема",
    )

    assert theme_dropdown is not None
    theme_dropdown.value = "dark"
    asyncio.run(theme_dropdown.on_select(None))

    assert page.theme_mode == ft.ThemeMode.DARK
    assert page.bgcolor == "#0F172A"
    pause_button = _find_control(
        page.added[0],
        lambda control: isinstance(control, ft.FilledButton)
        and control.content == "Пауза",
    )
    cancel_button = _find_control(
        page.added[0],
        lambda control: isinstance(control, ft.FilledButton)
        and control.content == "Скасувати друк",
    )
    assert pause_button.style.bgcolor == "#FBBF24"
    assert cancel_button.style.bgcolor == "#F87171"


def test_flet_page_resize_updates_bed_canvas_dimensions():
    import flet as ft

    page = FakePage()
    asyncio.run(app_main(page))
    bed_stack = _find_control(
        page.added[0],
        lambda control: isinstance(control, ft.Stack),
    )

    page.width = 390
    page.on_resize(None)

    assert bed_stack.width == 298
    assert bed_stack.height == 298
