"""Desktop Flet interface for local Bambu printer control."""
from __future__ import annotations

import asyncio
import ftplib
import logging
import tempfile
import uuid
import zipfile
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

import flet as ft
import flet.canvas as cv

from core.config import load_profiles, save_profiles
from core.ftp_client import PrinterFtp
from core import limits
from core.limits import CommandFailed, CommandRefused
from core.models import PrinterProfile
from core.printer import PrinterClient
from core.threemf import (
    ObjectLabelMatch,
    PlateObject,
    match_gcode_labels_to_objects,
    parse_3mf_gcode_labels,
    parse_3mf_objects,
)

log = logging.getLogger(__name__)

BED_SIZES_MM = {"A1MINI": 180.0, "A1": 256.0}
BED_VIEW_SIZE = 460.0
BED_VIEW_MIN_SIZE = 240.0
ACTIVE_PRINT_STATES = {"RUNNING", "PAUSE"}
OBJECT_LAYER_COLORS = (
    ("#CCFBF1", "#99F6E4", "#5EEAD4", "#2DD4BF", "#0F766E"),
    ("#DBEAFE", "#BFDBFE", "#93C5FD", "#60A5FA", "#2563EB"),
    ("#FFEDD5", "#FED7AA", "#FDBA74", "#FB923C", "#C2410C"),
    ("#EDE9FE", "#DDD6FE", "#C4B5FD", "#A78BFA", "#7C3AED"),
    ("#FCE7F3", "#FBCFE8", "#F9A8D4", "#F472B6", "#BE185D"),
    ("#ECFCCB", "#D9F99D", "#BEF264", "#A3E635", "#4D7C0F"),
)
THEME_PALETTES = {
    "light": {
        "page": "#F1F5F9",
        "surface": "#FFFFFF",
        "surface_alt": "#F8FAFC",
        "surface_selected": "#DBEAFE",
        "surface_skipped": "#F1F5F9",
        "text": "#0F172A",
        "muted": "#64748B",
        "border": "#E2E8F0",
        "bed": "#F8FAFC",
        "bed_border": "#64748B",
        "selected": "#2563EB",
        "skipped": "#64748B",
        "progress": "#0F766E",
        "track": "#E2E8F0",
        "warning": "#B45309",
        "running_bg": "#DCFCE7",
        "running_text": "#166534",
        "paused_bg": "#FEF3C7",
        "paused_text": "#92400E",
        "error_bg": "#FEE2E2",
        "error_text": "#991B1B",
        "online": "#15803D",
        "pause_button": "#FCD34D",
        "pause_button_text": "#422006",
        "resume_button": "#15803D",
        "resume_button_text": "#FFFFFF",
        "cancel_button": "#B91C1C",
        "cancel_button_text": "#FFFFFF",
    },
    "dark": {
        "page": "#0F172A",
        "surface": "#1E293B",
        "surface_alt": "#334155",
        "surface_selected": "#1E3A5F",
        "surface_skipped": "#334155",
        "text": "#F8FAFC",
        "muted": "#CBD5E1",
        "border": "#475569",
        "bed": "#111827",
        "bed_border": "#94A3B8",
        "selected": "#60A5FA",
        "skipped": "#94A3B8",
        "progress": "#2DD4BF",
        "track": "#334155",
        "warning": "#FBBF24",
        "running_bg": "#14532D",
        "running_text": "#BBF7D0",
        "paused_bg": "#713F12",
        "paused_text": "#FEF08A",
        "error_bg": "#7F1D1D",
        "error_text": "#FECACA",
        "online": "#4ADE80",
        "pause_button": "#FBBF24",
        "pause_button_text": "#422006",
        "resume_button": "#4ADE80",
        "resume_button_text": "#052E16",
        "cancel_button": "#F87171",
        "cancel_button_text": "#450A0A",
    },
}


def _effective_theme(mode: ft.ThemeMode, platform_brightness: ft.Brightness | None) -> str:
    if mode == ft.ThemeMode.DARK:
        return "dark"
    if mode == ft.ThemeMode.LIGHT:
        return "light"
    return "dark" if platform_brightness == ft.Brightness.DARK else "light"


def _bed_view_size(page_width: float | None) -> float:
    if page_width is None or page_width <= 0:
        return BED_VIEW_SIZE
    return min(BED_VIEW_SIZE, max(BED_VIEW_MIN_SIZE, page_width - 92))


def _format_remaining_time(value: Any) -> str:
    try:
        minutes = max(0, int(float(value)))
    except (TypeError, ValueError):
        return "—"
    hours, remaining_minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} год {remaining_minutes} хв" if remaining_minutes else f"{hours} год"
    return f"{remaining_minutes} хв"


def _estimated_finish_time(value: Any) -> str:
    try:
        minutes = max(0, int(float(value)))
    except (TypeError, ValueError):
        return "Завершення: —"
    return f"Завершення ~{datetime.now() + timedelta(minutes=minutes):%H:%M}"


def _printer_state_label(state: Any) -> str:
    return {
        "RUNNING": "Друкує",
        "PAUSE": "На паузі",
        "PREPARE": "Підготовка",
        "IDLE": "Готовий",
        "FINISH": "Завершив",
        "FAILED": "Помилка",
        "ERROR": "Помилка",
    }.get(str(state), "Стан невідомий")


def _job_file(print_state: dict[str, Any]) -> str | None:
    value = print_state.get("gcode_file") or print_state.get("subtask_name")
    if not isinstance(value, str) or not value.strip():
        return None
    filename = PurePosixPath(value.replace("\\", "/")).name
    if not filename.lower().endswith(".3mf"):
        return None
    return filename


def _reported_skip_label_ids(value: Any) -> set[int]:
    if not isinstance(value, list):
        return set()

    skipped: set[int] = set()
    for item in value:
        candidate = item
        if isinstance(item, dict):
            candidate = item.get("label_id", item.get("obj_id", item.get("id")))
        if isinstance(candidate, bool):
            continue
        try:
            skipped.add(int(candidate))
        except (TypeError, ValueError):
            continue
    return skipped


def _print_control_availability(
    state: str | None,
    connected: bool,
    busy: bool,
) -> tuple[bool, bool, bool]:
    if not connected or busy:
        return False, False, False
    return (
        state == "RUNNING",
        state == "PAUSE",
        state in {"RUNNING", "PAUSE", "PREPARE"},
    )


def _bed_rect(
    obj: PlateObject,
    bed_size_mm: float,
    view_size: float = BED_VIEW_SIZE,
) -> tuple[float, float, float, float]:
    x_min, y_min, x_max, y_max = obj.bbox
    scale = view_size / bed_size_mm
    left = max(0.0, min(view_size, x_min * scale))
    top = max(0.0, min(view_size, (bed_size_mm - y_max) * scale))
    right = max(left, min(view_size, x_max * scale))
    bottom = max(top, min(view_size, (bed_size_mm - y_min) * scale))
    return left, top, right - left, bottom - top


def _bed_canvas_shapes(
    objects: list[PlateObject],
    matches: list[ObjectLabelMatch],
    paths_by_label: dict[
        tuple[int, int],
        tuple[tuple[tuple[tuple[float, float], ...], ...], ...],
    ],
    bed_size_mm: float,
    reported_skip_ids: set[int],
    selected_layer: int = 1,
    overlay_layers: bool = False,
    view_size: float = BED_VIEW_SIZE,
    theme: str = "light",
) -> list[cv.Shape]:
    palette = THEME_PALETTES[theme]
    scale = view_size / bed_size_mm
    shapes: list[cv.Shape] = [
        cv.Rect(
            0,
            0,
            view_size,
            view_size,
            paint=ft.Paint(color=palette["bed"], style=ft.PaintingStyle.FILL),
        ),
        cv.Rect(
            1,
            1,
            view_size - 2,
            view_size - 2,
            paint=ft.Paint(
                color=palette["bed_border"],
                stroke_width=2,
                style=ft.PaintingStyle.STROKE,
            ),
        ),
    ]
    for object_index, obj in enumerate(objects):
        match = _match_for_object(obj, matches)
        if match is None:
            continue
        layer_paths = paths_by_label.get((obj.plate_number, match.label_id), ())
        visible_layers = (
            range(min(selected_layer, len(layer_paths)))
            if overlay_layers
            else (selected_layer - 1,)
        )
        palette = OBJECT_LAYER_COLORS[object_index % len(OBJECT_LAYER_COLORS)]
        for layer_index in visible_layers:
            if not 0 <= layer_index < len(layer_paths):
                continue
            color = (
                palette["skipped"]
                if match.label_id in reported_skip_ids
                else palette[min(layer_index, len(palette) - 1)]
                if overlay_layers
                else palette[-1]
            )
            for path in layer_paths[layer_index]:
                if len(path) < 2:
                    continue
                points = [
                    (x * scale, (bed_size_mm - y) * scale)
                    for x, y in path
                ]
                elements: list[cv.Path.PathElement] = [
                    cv.Path.MoveTo(points[0][0], points[0][1]),
                    *(cv.Path.LineTo(x, y) for x, y in points[1:]),
                ]
                shapes.append(
                    cv.Path(
                        elements=elements,
                        paint=ft.Paint(
                            color=color,
                            stroke_width=2.0,
                            stroke_cap=ft.StrokeCap.ROUND,
                            stroke_join=ft.StrokeJoin.ROUND,
                            style=ft.PaintingStyle.STROKE,
                        ),
                    )
                )
    return shapes


def _available_object_layers(
    objects: list[PlateObject],
    matches: list[ObjectLabelMatch],
    paths_by_label: dict[
        tuple[int, int],
        tuple[tuple[tuple[tuple[float, float], ...], ...], ...],
    ],
) -> int:
    available = 0
    for obj in objects:
        match = _match_for_object(obj, matches)
        if match is None:
            continue
        layers = paths_by_label.get((obj.plate_number, match.label_id), ())
        contiguous_layers = 0
        for layer_paths in layers[:5]:
            if not any(len(path) > 1 for path in layer_paths):
                break
            contiguous_layers += 1
        available = max(available, contiguous_layers)
    return available


def _download_job(profile: PrinterProfile, remote_name: str, local_path: Path) -> None:
    with PrinterFtp(profile) as ftp:
        ftp.download(f"/{remote_name}", local_path)


def _match_for_object(
    obj: PlateObject,
    matches: list[ObjectLabelMatch],
) -> ObjectLabelMatch | None:
    for match in matches:
        if match.plate_number == obj.plate_number and match.object_id == obj.id:
            return match
    return None


def _can_skip_selected_labels(
    selected_label_ids: set[int],
    matches: list[ObjectLabelMatch],
    reported_skip_ids: set[int],
) -> bool:
    active_label_ids = {
        match.label_id
        for match in matches
        if match.label_id not in reported_skip_ids
    }
    return (
        bool(selected_label_ids)
        and selected_label_ids <= active_label_ids
        and len(selected_label_ids) < len(active_label_ids)
    )


async def app_main(page: ft.Page) -> None:
    page.title = "BambuDeck — LAN"
    theme_mode = ft.ThemeMode.SYSTEM
    effective_theme = _effective_theme(theme_mode, page.platform_brightness)
    theme_colors = THEME_PALETTES[effective_theme]
    page.theme_mode = theme_mode
    page.bgcolor = theme_colors["page"]
    page.padding = 20

    client: PrinterClient | None = None
    active_profile: PrinterProfile | None = None
    active_profile_id: str | None = None
    clients_by_profile: dict[str, PrinterClient] = {}
    connecting_profiles: set[str] = set()
    connection_errors: dict[str, str] = {}
    profiles: list[PrinterProfile] = []
    objects: list[PlateObject] = []
    matches: list[ObjectLabelMatch] = []
    selected_skip_label_ids: set[int] = set()
    current_bed_view_size = _bed_view_size(page.width)
    paths_by_label: dict[
        tuple[int, int],
        tuple[tuple[tuple[tuple[float, float], ...], ...], ...],
    ] = {}
    available_layer_count = 1
    selected_layer = 1
    overlay_layers = False
    loaded_job_key: tuple[str, str, str] | None = None
    attempted_job_key: tuple[str, str, str] | None = None
    temporary_directory = tempfile.TemporaryDirectory(prefix="bambudeck-ui-")
    disposed = False
    busy = False

    connection_text = ft.Text("Не підключено", color=theme_colors["muted"])
    message_text = ft.Text(
        "Додай принтер або вибери картку для керування.",
        color=theme_colors["muted"],
    )
    active_profile_heading = ft.Text(
        "Керування принтером",
        size=20,
        weight=ft.FontWeight.BOLD,
        color=theme_colors["text"],
    )
    printer_state_text = ft.Text("—", size=15, weight=ft.FontWeight.BOLD)
    state_badge = ft.Container(
        content=printer_state_text,
        padding=ft.Padding.symmetric(horizontal=12, vertical=7),
        bgcolor=theme_colors["surface_alt"],
        border_radius=20,
    )
    job_text = ft.Text("Завдання: —")
    progress_percent_text = ft.Text("—%", size=24, weight=ft.FontWeight.BOLD)
    progress_text = ft.Text("Шар — · залишок —")
    nozzle_text = ft.Text("Сопло: —", size=13)
    bed_text = ft.Text("Стіл: —", size=13)
    skipped_text = ft.Text("Звіт s_obj (label IDs): —")
    selection_summary_text = ft.Text(
        "Натисніть на об'єкти, щоб вибрати їх для пропуску.",
        color=theme_colors["text"],
    )
    selection_hint_text = ft.Text(
        "Залиште щонайменше один об'єкт для друку.",
        size=12,
        color=theme_colors["warning"],
    )
    skip_selected_button = ft.FilledButton(
        content="Пропустити вибрані (0)",
        disabled=True,
    )
    progress_bar = ft.ProgressBar(
        value=0,
        color=theme_colors["progress"],
        bgcolor=theme_colors["track"],
        bar_height=8,
        border_radius=8,
    )
    bed_stack = ft.Stack(
        width=current_bed_view_size,
        height=current_bed_view_size,
        clip_behavior=ft.ClipBehavior.HARD_EDGE,
    )
    object_list = ft.Column(spacing=8)
    layer_dropdown = ft.Dropdown(
        label="Шар",
        value="1",
        options=[ft.DropdownOption(key="1", text="Шар 1")],
        width=140,
        disabled=True,
    )
    overlay_checkbox = ft.Checkbox(
        label="Накласти попередні шари",
        value=False,
        disabled=True,
    )
    busy_indicator = ft.ProgressRing(visible=False, width=18, height=18, stroke_width=2)
    theme_dropdown = ft.Dropdown(
        label="Тема",
        value="system",
        options=[
            ft.DropdownOption(key="system", text="Системна"),
            ft.DropdownOption(key="light", text="Світла"),
            ft.DropdownOption(key="dark", text="Темна"),
        ],
        width=150,
    )
    printable_swatch = ft.Container(
        width=12,
        height=12,
        bgcolor="#99F6E4",
        border_radius=6,
    )
    selected_swatch = ft.Container(
        width=12,
        height=12,
        bgcolor=theme_colors["selected"],
        border_radius=6,
    )
    skipped_swatch = ft.Container(
        width=12,
        height=12,
        bgcolor=theme_colors["skipped"],
        border_radius=6,
    )
    bed_legend = ft.Row(
        [
            ft.Row(
                [
                    printable_swatch,
                    ft.Text("Друкується", size=12, color=theme_colors["muted"]),
                ],
                spacing=6,
            ),
            ft.Row(
                [
                    selected_swatch,
                    ft.Text("Вибрано", size=12, color=theme_colors["muted"]),
                ],
                spacing=6,
            ),
            ft.Row(
                [
                    skipped_swatch,
                    ft.Text("Пропущено", size=12, color=theme_colors["muted"]),
                ],
                spacing=6,
            ),
            ft.Text("Відтінки показують шари", size=12, color=theme_colors["muted"]),
        ],
        wrap=True,
        spacing=16,
        run_spacing=8,
    )

    try:
        profiles = load_profiles()
    except (OSError, ValueError, TypeError) as exc:
        log.exception("Could not load printer profiles")
        message_text.value = f"Не вдалося завантажити printers.json: {exc}"

    refresh_button = ft.OutlinedButton("Оновити", disabled=True)
    pause_button = ft.FilledButton(
        "Пауза",
        visible=False,
        disabled=True,
        style=ft.ButtonStyle(
            bgcolor=theme_colors["pause_button"],
            color=theme_colors["pause_button_text"],
        ),
    )
    resume_button = ft.FilledButton(
        "Продовжити",
        visible=False,
        disabled=True,
        style=ft.ButtonStyle(
            bgcolor=theme_colors["resume_button"],
            color=theme_colors["resume_button_text"],
        ),
    )
    stop_button = ft.FilledButton(
        "Скасувати друк",
        visible=False,
        disabled=True,
        style=ft.ButtonStyle(
            bgcolor=theme_colors["cancel_button"],
            color=theme_colors["cancel_button_text"],
        ),
    )
    add_printer_button = ft.FilledButton("Додати принтер")
    printer_cards_column = ft.Column(spacing=10, tight=True)
    details_panel = ft.Column(visible=False, spacing=12, tight=True)
    collapse_details_button = ft.TextButton("Згорнути керування")

    async def show_skip_confirmation(label_ids: list[int]) -> None:
        nonlocal client
        selected_targets = [
            (obj, match)
            for obj in objects
            if (match := _match_for_object(obj, matches)) is not None
            and match.label_id in label_ids
        ]
        target_details = "\n".join(
            f"• {obj.name} · JSON ID {obj.id} · label {match.label_id}"
            for obj, match in selected_targets
        )
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"Пропустити вибрані об'єкти ({len(selected_targets)})?"),
            content=ft.Text(
                f"{target_details}\n\n"
                "LAN-пропуск перевірено на A1 mini, але поведінка інших моделей і версій "
                "прошивки може відрізнятися. ACK success і поява ID у s_obj не є "
                "самостійним доказом фізичного пропуску. "
                "Дію не можна відкотити. Якщо принтер уже почав друкувати вибраний об'єкт, "
                "команда може запізнитися. Перевіряйте лише на тестовому друці, "
                "який можна перервати."
            ),
            actions_alignment=ft.MainAxisAlignment.END,
        )

        async def cancel(_: Any) -> None:
            page.pop_dialog()

        async def confirm(_: Any) -> None:
            nonlocal client
            page.pop_dialog()
            if client is None:
                message_text.value = "Немає активного з'єднання з принтером."
                page.update()
                return
            latest = client.snapshot().get("print", {})
            if latest.get("gcode_state") != "RUNNING":
                message_text.value = "Скасування дозволене лише під час активного друку."
                page.update()
                return
            latest_file = _job_file(latest)
            latest_task = str(latest.get("task_id") or "")
            expected_key = (
                active_profile.serial if active_profile else "",
                latest_file or "",
                latest_task,
            )
            if latest_file is None or expected_key != loaded_job_key:
                message_text.value = "Активне завдання змінилося. Оновіть дані перед дією."
                page.update()
                return
            reported_ids = _reported_skip_label_ids(latest.get("s_obj"))
            if not _can_skip_selected_labels(set(label_ids), matches, reported_ids):
                message_text.value = (
                    "Вибір змінився або містить усі об'єкти, що ще друкуються. "
                    "Оновіть вибір; останній об'єкт не можна пропустити."
                )
                page.update()
                return

            message_text.value = (
                f"Надсилаю команду для {len(label_ids)} об'єкт(ів): "
                f"{', '.join(str(label_id) for label_id in label_ids)}…"
            )
            reported_before = latest.get("s_obj")
            log.info(
                "Requesting skip_objects for labels %s (task_id=%s, s_obj_before=%r)",
                label_ids,
                latest_task,
                reported_before,
            )
            page.update()
            try:
                ack = await asyncio.to_thread(client.skip_objects, label_ids)
                confirmed = await asyncio.to_thread(
                    client.wait_until,
                    lambda state: set(label_ids) <= _reported_skip_label_ids(state.get("s_obj")),
                    10.0,
                )
                if not confirmed:
                    await asyncio.to_thread(client.request_full_state)
                    confirmed = await asyncio.to_thread(
                        client.wait_until,
                        lambda state: set(label_ids) <= _reported_skip_label_ids(state.get("s_obj")),
                        5.0,
                    )
                reported_after = client.snapshot().get("print", {}).get("s_obj")
                log.info(
                    "skip_objects result for labels %s (task_id=%s, ack_result=%r, "
                    "ack_reason=%r, s_obj_after=%r, reported=%s)",
                    label_ids,
                    latest_task,
                    ack.get("result") if ack else None,
                    ack.get("reason") if ack else None,
                    reported_after,
                    confirmed,
                )
                if confirmed:
                    message_text.value = (
                        f"Принтер додав усі {len(label_ids)} label ID до s_obj. "
                        "Переконайтеся, що вибрані об'єкти фізично не друкуються."
                    )
                elif ack is None:
                    message_text.value = (
                        f"Команду надіслано, але принтер не підтвердив відповіддю чи s_obj "
                        f"усі ID: {label_ids}."
                    )
                else:
                    message_text.value = (
                        f"Принтер відповів, але s_obj не підтвердив усі ID {label_ids}: "
                        f"{ack.get('result', 'відповідь отримано')}."
                    )
                render_objects(client.snapshot().get("print", {}))
            except (CommandRefused, ConnectionError, RuntimeError, OSError) as exc:
                log.exception("Object skip command failed")
                message_text.value = f"Не вдалося пропустити об'єкт: {exc}"
            page.update()

        dialog.actions = [
            ft.TextButton("Назад", on_click=cancel),
            ft.FilledButton("Пропустити вибрані", on_click=confirm),
        ]
        page.show_dialog(dialog)

    def toggle_skip_selection(label_id: int) -> None:
        if label_id in selected_skip_label_ids:
            selected_skip_label_ids.remove(label_id)
        else:
            selected_skip_label_ids.add(label_id)
        print_state = client.snapshot().get("print", {}) if client is not None else {}
        render_objects(print_state)
        page.update()

    async def skip_selected_clicked(_: Any) -> None:
        reported_ids = _reported_skip_label_ids(
            client.snapshot().get("print", {}).get("s_obj")
            if client is not None else None
        )
        if _can_skip_selected_labels(selected_skip_label_ids, matches, reported_ids):
            await show_skip_confirmation(sorted(selected_skip_label_ids))
        else:
            message_text.value = (
                "Виберіть щонайменше один доступний об'єкт, але залиште один "
                "об'єкт для продовження друку."
            )
            page.update()

    def render_objects(print_state: dict[str, Any]) -> None:
        nonlocal objects, matches, current_bed_view_size
        model = active_profile.model if active_profile else "A1MINI"
        bed_size = BED_SIZES_MM.get(model)
        reported_skip_ids = _reported_skip_label_ids(print_state.get("s_obj"))
        skip_flag = limits.object_skip_flag(print_state.get("fun"))
        is_running = print_state.get("gcode_state") == "RUNNING"
        mapping = {item.object_id: item for item in matches}
        has_object_layout = bool(objects) and bed_size is not None
        objects_row.visible = has_object_layout
        bed_card.visible = has_object_layout
        bed_surface.visible = has_object_layout

        current_bed_view_size = _bed_view_size(page.width)
        bed_stack.width = current_bed_view_size
        bed_stack.height = current_bed_view_size
        if not has_object_layout:
            bed_stack.controls = []
            object_list.controls = []
            return

        bed_stack.controls = [
            cv.Canvas(
                width=current_bed_view_size,
                height=current_bed_view_size,
                shapes=_bed_canvas_shapes(
                    objects,
                    matches,
                    paths_by_label,
                    bed_size,
                    reported_skip_ids,
                    selected_layer,
                    overlay_layers,
                    current_bed_view_size,
                    effective_theme,
                ),
            )
        ]
        rows: list[ft.Control] = []
        active_count = sum(1 for item in matches if item.label_id not in reported_skip_ids)
        can_skip = is_running and active_count > 1
        selected_skip_label_ids.intersection_update(
            item.label_id for item in matches
            if item.label_id not in reported_skip_ids
        )
        selected_names = [
            obj.name
            for obj in objects
            if (match := _match_for_object(obj, matches)) is not None
            and match.label_id in selected_skip_label_ids
        ]
        if selected_names:
            selection_summary_text.value = (
                f"Вибрано {len(selected_skip_label_ids)}: "
                + ", ".join(selected_names)
            )
        else:
            selection_summary_text.value = (
                "Натисніть на об'єкти, щоб вибрати їх для пропуску."
            )
        selection_hint_text.value = (
            f"Можна вибрати до {max(active_count - 1, 0)} об'єкт(ів): "
            "щонайменше один об'єкт має залишитися для друку."
        )
        can_submit_selection = (
            is_running
            and _can_skip_selected_labels(
                selected_skip_label_ids,
                matches,
                reported_skip_ids,
            )
        )
        skip_selected_button.content = (
            f"Пропустити вибрані ({len(selected_skip_label_ids)})"
        )
        skip_selected_button.disabled = not can_submit_selection

        for object_index, obj in enumerate(objects):
            match = _match_for_object(obj, matches)
            is_reported_skip = match is not None and match.label_id in reported_skip_ids
            is_selected = match is not None and match.label_id in selected_skip_label_ids
            left, top, width, height = _bed_rect(
                obj,
                bed_size,
                current_bed_view_size,
            )
            if width <= 0 or height <= 0:
                continue

            if is_reported_skip:
                color, border_color = theme_colors["surface_skipped"], theme_colors["skipped"]
            elif match is None:
                color, border_color = theme_colors["surface_alt"], theme_colors["muted"]
            else:
                color, border_color = theme_colors["surface"], theme_colors["progress"]
            has_silhouette = (
                match is not None
                and any(
                    len(path) > 1
                    for layer_paths in paths_by_label.get(
                        (obj.plate_number, match.label_id), ()
                    )
                    for path in layer_paths
                )
            )
            if has_silhouette:
                color = "transparent"

            label_text = f"#{obj.id}"
            if match is not None:
                label_text = f"#{match.label_id}"
            caption = f"{obj.name}\n{label_text}"
            rect = ft.Container(
                left=left,
                top=top,
                width=width,
                height=height,
                on_click=(
                    (lambda _, object_label=match.label_id:
                     toggle_skip_selection(object_label))
                    if match is not None and can_skip and not is_reported_skip
                    else None
                ),
                bgcolor=color,
                border=ft.Border.all(
                    3 if is_selected else 2,
                    theme_colors["selected"] if is_selected else border_color,
                ),
                border_radius=5,
                padding=3,
                alignment=ft.Alignment.CENTER,
                tooltip=(
                    f"{obj.name} · JSON ID {obj.id} · "
                    f"G-code label {match.label_id if match else 'не визначено'}"
                ),
                ink=match is not None and can_skip and not is_reported_skip,
                content=ft.Text(
                    caption,
                    size=10,
                    text_align=ft.TextAlign.CENTER,
                    color=theme_colors["muted"] if is_reported_skip else theme_colors["text"],
                    weight=ft.FontWeight.BOLD,
                    max_lines=2,
                    overflow=ft.TextOverflow.ELLIPSIS,
                ),
            )
            bed_stack.controls.append(rect)

            detail = f"JSON ID {obj.id}"
            if match is not None:
                detail += f" · label {match.label_id}"
            else:
                detail += " · label не зіставлено"
            if is_reported_skip:
                detail += " · label у звіті s_obj"
            rows.append(
                ft.Container(
                    content=ft.Row(
                        [
                            ft.Checkbox(
                                value=is_selected,
                                disabled=(
                                    match is None
                                    or not can_skip
                                    or is_reported_skip
                                ),
                                on_change=(
                                    (lambda _, object_label=match.label_id:
                                     toggle_skip_selection(object_label))
                                    if match is not None and can_skip and not is_reported_skip
                                    else None
                                ),
                            ),
                            ft.Column(
                                [
                                    ft.Text(obj.name, weight=ft.FontWeight.BOLD),
                                    ft.Text(detail, size=12, color=theme_colors["muted"]),
                                ],
                                spacing=2,
                                tight=True,
                            ),
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    padding=10,
                    border=ft.Border.all(
                        2 if is_selected else 1,
                        theme_colors["selected"] if is_selected else theme_colors["border"],
                    ),
                    border_radius=8,
                    bgcolor=(
                        theme_colors["surface_selected"]
                        if is_selected
                        else theme_colors["surface_skipped"]
                        if is_reported_skip
                        else OBJECT_LAYER_COLORS[
                            object_index % len(OBJECT_LAYER_COLORS)
                        ][0]
                        if match is not None
                        else theme_colors["surface_alt"]
                    ),
                )
            )

        object_list.controls = rows or [ft.Text("У 3MF не знайдено об'єктів.")]

    def render_printer_cards() -> None:
        cards: list[ft.Control] = []
        for profile in profiles:
            printer_client = clients_by_profile.get(profile.id)
            print_state = (
                printer_client.snapshot().get("print", {})
                if printer_client is not None else {}
            )
            state = print_state.get("gcode_state")
            percent_raw = print_state.get("mc_percent")
            try:
                percent = (
                    max(0.0, min(100.0, float(percent_raw)))
                    if percent_raw is not None else 0.0
                )
            except (TypeError, ValueError):
                percent = 0.0

            if profile.id in connecting_profiles:
                status = "Підключення…"
                status_bg = theme_colors["surface_alt"]
                status_text_color = theme_colors["muted"]
            elif printer_client is None and profile.id in connection_errors:
                status = "Не вдалося підключитися"
                status_bg = theme_colors["error_bg"]
                status_text_color = theme_colors["error_text"]
            elif printer_client is None and not profile.enabled:
                status = "Вимкнено"
                status_bg = theme_colors["surface_alt"]
                status_text_color = theme_colors["muted"]
            elif printer_client is None:
                status = "Не підключено"
                status_bg = theme_colors["surface_alt"]
                status_text_color = theme_colors["muted"]
            elif not printer_client.online:
                status = "Немає звіту"
                status_bg = theme_colors["paused_bg"]
                status_text_color = theme_colors["paused_text"]
            else:
                status = _printer_state_label(state)
                status_bg = (
                    theme_colors["running_bg"] if state == "RUNNING"
                    else theme_colors["paused_bg"] if state == "PAUSE"
                    else theme_colors["error_bg"] if state in {"FAILED", "ERROR"}
                    else theme_colors["surface_alt"]
                )
                status_text_color = (
                    theme_colors["running_text"] if state == "RUNNING"
                    else theme_colors["paused_text"] if state == "PAUSE"
                    else theme_colors["error_text"] if state in {"FAILED", "ERROR"}
                    else theme_colors["text"]
                )

            is_printing = state in ACTIVE_PRINT_STATES
            job_name = (
                print_state.get("subtask_name")
                or print_state.get("gcode_file")
                or "Немає активного завдання"
            )
            if is_printing:
                print_summary = (
                    f"{percent:g}% · "
                    f"{_format_remaining_time(print_state.get('mc_remaining_time'))} "
                    f"до завершення · "
                    f"{_estimated_finish_time(print_state.get('mc_remaining_time'))}"
                )
            else:
                print_summary = connection_errors.get(
                    profile.id,
                    f"{profile.ip} · {profile.model}",
                )

            async def card_clicked(_: Any, profile_id: str = profile.id) -> None:
                await select_profile(profile_id)

            async def connection_clicked(
                _: Any,
                selected_profile: PrinterProfile = profile,
            ) -> None:
                if selected_profile.id in clients_by_profile:
                    await disconnect_profile(selected_profile)
                else:
                    await connect_profile(selected_profile)

            async def edit_clicked(
                _: Any,
                selected_profile: PrinterProfile = profile,
            ) -> None:
                await show_profile_dialog(selected_profile)

            async def delete_clicked(
                _: Any,
                selected_profile: PrinterProfile = profile,
            ) -> None:
                await confirm_delete_profile(selected_profile)

            connect_label = (
                "Підключення…"
                if profile.id in connecting_profiles
                else "Відключити"
                if printer_client is not None
                else "Повторити"
                if profile.id in connection_errors
                else "Підключити"
            )
            connect_disabled = (
                profile.id in connecting_profiles
                or (not profile.enabled and printer_client is None)
            )
            card = ft.Container(
                content=ft.Column(
                    [
                        ft.Row(
                            [
                                ft.Column(
                                    [
                                        ft.Text(
                                            profile.name,
                                            size=17,
                                            weight=ft.FontWeight.BOLD,
                                            color=theme_colors["text"],
                                        ),
                                        ft.Text(
                                            f"{profile.model if profile.model != 'UNKNOWN' else 'Модель не вказана'} · {profile.ip}",
                                            size=12,
                                            color=theme_colors["muted"],
                                        ),
                                    ],
                                    spacing=2,
                                    tight=True,
                                ),
                                ft.Container(
                                    content=ft.Text(
                                        status,
                                        size=12,
                                        weight=ft.FontWeight.BOLD,
                                        color=status_text_color,
                                    ),
                                    bgcolor=status_bg,
                                    padding=ft.Padding.symmetric(horizontal=10, vertical=6),
                                    border_radius=16,
                                ),
                            ],
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            wrap=True,
                        ),
                        ft.ProgressBar(
                            value=percent / 100 if is_printing else 0,
                            color=theme_colors["progress"],
                            bgcolor=theme_colors["track"],
                            bar_height=6,
                            border_radius=6,
                        ),
                        ft.Row(
                            [
                                ft.Column(
                                    [
                                        ft.Text(
                                            job_name,
                                            size=13,
                                            color=theme_colors["text"],
                                            max_lines=1,
                                            overflow=ft.TextOverflow.ELLIPSIS,
                                        ),
                                        ft.Text(
                                            print_summary,
                                            size=12,
                                            color=theme_colors["muted"],
                                        ),
                                    ],
                                    spacing=2,
                                    tight=True,
                                ),
                                ft.OutlinedButton(
                                    connect_label,
                                    on_click=connection_clicked,
                                    disabled=connect_disabled,
                                ),
                                ft.TextButton(
                                    "Змінити",
                                    on_click=edit_clicked,
                                    disabled=profile.id in connecting_profiles
                                    or (busy and profile.id == active_profile_id),
                                ),
                                ft.TextButton(
                                    "Видалити",
                                    on_click=delete_clicked,
                                    disabled=profile.id in connecting_profiles
                                    or (busy and profile.id == active_profile_id),
                                ),
                            ],
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            wrap=True,
                            spacing=6,
                        ),
                    ],
                    spacing=8,
                    tight=True,
                ),
                padding=14,
                bgcolor=theme_colors["surface"],
                border_radius=14,
                border=ft.Border.all(
                    2 if profile.id == active_profile_id else 1,
                    theme_colors["selected"]
                    if profile.id == active_profile_id
                    else theme_colors["border"],
                ),
                on_click=card_clicked,
            )
            cards.append(card)
        printer_cards_column.controls = cards or [
            ft.Text("Додай принтер, щоб почати.", color=theme_colors["muted"])
        ]

    def render_status(print_state: dict[str, Any]) -> None:
        state = print_state.get("gcode_state", "—")
        skip_flag = limits.object_skip_flag(print_state.get("fun"))
        percent_value = print_state.get("mc_percent")
        try:
            percent = max(0.0, min(100.0, float(percent_value))) if percent_value is not None else 0.0
        except (TypeError, ValueError):
            percent = 0.0
        progress_bar.value = percent / 100
        progress_percent_text.value = f"{percent:g}%"
        printer_state_text.value = str(state)
        state_badge.bgcolor = (
            theme_colors["running_bg"] if state == "RUNNING"
            else theme_colors["paused_bg"] if state == "PAUSE"
            else theme_colors["error_bg"] if state in {"FAILED", "ERROR"}
            else theme_colors["surface_alt"]
        )
        printer_state_text.color = (
            theme_colors["running_text"] if state == "RUNNING"
            else theme_colors["paused_text"] if state == "PAUSE"
            else theme_colors["error_text"] if state in {"FAILED", "ERROR"}
            else theme_colors["text"]
        )
        job_text.value = f"Завдання: {print_state.get('subtask_name') or print_state.get('gcode_file') or '—'}"
        progress_text.value = (
            f"Шар {print_state.get('layer_num', '—')}/"
            f"{print_state.get('total_layer_num', '—')} · залишок "
            f"{_format_remaining_time(print_state.get('mc_remaining_time'))}"
        )
        nozzle_text.value = (
            f"Сопло: {print_state.get('nozzle_temper', '—')} / "
            f"{print_state.get('nozzle_target_temper', '—')} °C"
        )
        bed_text.value = (
            f"Стіл: {print_state.get('bed_temper', '—')} / "
            f"{print_state.get('bed_target_temper', '—')} °C"
        )
        skipped_text.value = (
            "print.fun bit 49 (значення для skip не підтверджено): "
            f"{'1' if skip_flag is True else '0' if skip_flag is False else 'не передано'}"
            f" · s_obj (повідомлені IDs, не доказ фізичного пропуску): "
            f"{print_state.get('s_obj') or '—'}"
        )
        if client is not None and client.online:
            connection_text.value = f"ONLINE · {active_profile.name if active_profile else ''}"
            connection_text.color = theme_colors["online"]
        elif client is not None:
            connection_text.value = "Підключено MQTT, звіти застаріли"
            connection_text.color = theme_colors["warning"]
        else:
            connection_text.color = theme_colors["muted"]
        refresh_button.disabled = client is None or busy
        pause_enabled, resume_enabled, stop_enabled = _print_control_availability(
            state if isinstance(state, str) else None,
            client is not None,
            busy,
        )
        pause_button.visible = state == "RUNNING"
        pause_button.disabled = not pause_enabled
        resume_button.visible = state == "PAUSE"
        resume_button.disabled = not resume_enabled
        stop_button.visible = state in {"RUNNING", "PAUSE", "PREPARE"}
        stop_button.disabled = not stop_enabled
        render_objects(print_state)

    async def send_print_action(
        action: str,
        allowed_states: set[str],
        target_states: set[str],
        timeout: float,
    ) -> None:
        nonlocal busy
        current_client = client
        if current_client is None:
            message_text.value = "Немає активного з'єднання з принтером."
            page.update()
            return
        state = current_client.snapshot().get("print", {}).get("gcode_state")
        if state not in allowed_states:
            message_text.value = (
                f"Дія недоступна у стані {state or 'невідомо'}. "
                "Оновіть стан принтера."
            )
            render_status(current_client.snapshot().get("print", {}))
            page.update()
            return
        if busy:
            return

        busy = True
        message_text.value = "Надсилаю команду принтеру…"
        render_status(current_client.snapshot().get("print", {}))
        page.update()
        try:
            await asyncio.to_thread(getattr(current_client, action))
            confirmed = await asyncio.to_thread(
                current_client.wait_until,
                lambda print_state: print_state.get("gcode_state") in target_states,
                timeout,
            )
            if not confirmed:
                await asyncio.to_thread(current_client.request_full_state)
                confirmed = await asyncio.to_thread(
                    current_client.wait_until,
                    lambda print_state: print_state.get("gcode_state") in target_states,
                    5.0,
                )

            final_state = current_client.snapshot().get("print", {}).get("gcode_state")
            if confirmed:
                message_text.value = f"Стан принтера підтверджено: {final_state}."
            else:
                message_text.value = (
                    f"Команду {action} надіслано, але перехід стану не підтверджено "
                    f"(поточний стан: {final_state or 'невідомо'})."
                )
        except (CommandFailed, CommandRefused, ConnectionError, RuntimeError, OSError) as exc:
            log.exception("Print control command %s failed", action)
            message_text.value = f"Не вдалося виконати команду: {exc}"
        finally:
            busy = False
            render_status(current_client.snapshot().get("print", {}))
            page.update()

    async def pause_clicked(_: Any) -> None:
        await send_print_action("pause", {"RUNNING"}, {"PAUSE"}, 10.0)

    async def resume_clicked(_: Any) -> None:
        await send_print_action("resume", {"PAUSE"}, {"RUNNING"}, 10.0)

    async def stop_clicked(_: Any) -> None:
        current_state = (
            client.snapshot().get("print", {}).get("gcode_state")
            if client is not None else None
        )
        if client is None or current_state not in {"RUNNING", "PAUSE", "PREPARE"}:
            message_text.value = "Скасувати можна лише активне завдання друку."
            render_status(client.snapshot().get("print", {}) if client else {})
            page.update()
            return

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Скасувати поточний друк?"),
            content=ft.Text(
                "Друк буде перервано, і продовжити це завдання вже не вдасться."
            ),
            actions_alignment=ft.MainAxisAlignment.END,
        )

        async def cancel(_: Any) -> None:
            page.pop_dialog()

        async def confirm(_: Any) -> None:
            page.pop_dialog()
            await send_print_action(
                "stop",
                {"RUNNING", "PAUSE", "PREPARE"},
                {"FAILED", "IDLE", "FINISH"},
                15.0,
            )

        dialog.actions = [
            ft.TextButton("Скасувати", on_click=cancel),
            ft.FilledButton(
                "Так, скасувати друк",
                on_click=confirm,
                style=ft.ButtonStyle(
                    bgcolor=theme_colors["cancel_button"],
                    color=theme_colors["cancel_button_text"],
                ),
            ),
        ]
        page.show_dialog(dialog)

    def layer_changed(_: Any) -> None:
        nonlocal selected_layer
        try:
            selected_layer = int(layer_dropdown.value or "1")
        except ValueError:
            selected_layer = 1
        state = client.snapshot().get("print", {}) if client is not None else {}
        render_objects(state)
        page.update()

    def overlay_changed(_: Any) -> None:
        nonlocal overlay_layers
        overlay_layers = bool(overlay_checkbox.value)
        state = client.snapshot().get("print", {}) if client is not None else {}
        render_objects(state)
        page.update()

    async def load_job(profile: PrinterProfile, filename: str) -> None:
        nonlocal objects, matches, loaded_job_key, attempted_job_key
        nonlocal paths_by_label, available_layer_count, selected_layer, overlay_layers
        task_state = client.snapshot().get("print", {}) if client is not None else {}
        task_id = str(task_state.get("task_id") or "")
        job_key = (profile.serial, filename, task_id)
        if job_key == attempted_job_key:
            return
        attempted_job_key = job_key
        selected_skip_label_ids.clear()
        message_text.value = f"Завантажую {filename} та аналізую об'єкти…"
        page.update()
        local_path = Path(temporary_directory.name) / "current-job.3mf"
        try:
            await asyncio.to_thread(_download_job, profile, filename, local_path)
            parsed_objects = await asyncio.to_thread(parse_3mf_objects, local_path)
            parsed_labels = await asyncio.to_thread(parse_3mf_gcode_labels, local_path)
            parsed_matches = await asyncio.to_thread(
                match_gcode_labels_to_objects,
                parsed_objects,
                parsed_labels,
            )
            plate_numbers = {obj.plate_number for obj in parsed_objects}
            if len(plate_numbers) != 1:
                raise ValueError(
                    "У файлі кілька пластин; вибір активної пластини ще не реалізовано."
                )
            objects = parsed_objects
            matches = parsed_matches
            paths_by_label = {
                (plate.plate_number, label.label_id): label.layer_paths
                for plate in parsed_labels
                for label in plate.labels
            }
            available_layer_count = _available_object_layers(
                objects,
                matches,
                paths_by_label,
            )
            selected_layer = max(available_layer_count, 1)
            overlay_layers = available_layer_count > 1
            overlay_checkbox.value = overlay_layers
            layer_dropdown.options = (
                [
                    ft.DropdownOption(key=str(layer), text=f"Шар {layer}")
                    for layer in range(1, available_layer_count + 1)
                ]
                if available_layer_count
                else [ft.DropdownOption(key="1", text="Траєкторії не знайдені")]
            )
            layer_dropdown.value = str(selected_layer)
            layer_dropdown.disabled = available_layer_count <= 1
            overlay_checkbox.disabled = available_layer_count <= 1
            loaded_job_key = job_key
            if available_layer_count:
                message_text.value = (
                    f"Завантажено {len(objects)} об'єкт(и); "
                    f"знайдено траєкторії для {available_layer_count} перших шар(ів)."
                )
            else:
                message_text.value = (
                    f"Завантажено {len(objects)} об'єкт(и), але екструзійні траєкторії "
                    "перших шарів не розпізнані; показано лише bbox."
                )
        except (OSError, ValueError, RuntimeError, ConnectionError, ftplib.Error, zipfile.BadZipFile) as exc:
            log.exception("Could not load or analyze active 3MF job")
            objects = []
            matches = []
            paths_by_label = {}
            available_layer_count = 1
            selected_layer = 1
            layer_dropdown.options = [ft.DropdownOption(key="1", text="Шар 1")]
            layer_dropdown.value = "1"
            layer_dropdown.disabled = True
            overlay_checkbox.disabled = True
            loaded_job_key = None
            message_text.value = f"Не вдалося підготувати схему деталей: {exc}"
        page.update()

    async def refresh_from_client() -> None:
        nonlocal available_layer_count, selected_layer
        if client is None:
            return
        print_state = client.snapshot().get("print", {})
        render_status(print_state)
        filename = _job_file(print_state)
        state = print_state.get("gcode_state")
        if filename and state in ACTIVE_PRINT_STATES and active_profile is not None:
            task_id = str(print_state.get("task_id") or "")
            key = (active_profile.serial, filename, task_id)
            if key != loaded_job_key and key != attempted_job_key:
                await load_job(active_profile, filename)
        elif state not in ACTIVE_PRINT_STATES:
            objects.clear()
            matches.clear()
            selected_skip_label_ids.clear()
            paths_by_label.clear()
            available_layer_count = 1
            selected_layer = 1
            layer_dropdown.options = [ft.DropdownOption(key="1", text="Шар 1")]
            layer_dropdown.value = "1"
            layer_dropdown.disabled = True
            overlay_checkbox.disabled = True
            render_objects(print_state)
        page.update()

    def reset_selected_job() -> None:
        nonlocal objects, matches, paths_by_label, loaded_job_key, attempted_job_key
        nonlocal available_layer_count, selected_layer, overlay_layers
        objects = []
        matches = []
        selected_skip_label_ids.clear()
        paths_by_label = {}
        loaded_job_key = None
        attempted_job_key = None
        available_layer_count = 1
        selected_layer = 1
        overlay_layers = False
        layer_dropdown.options = [ft.DropdownOption(key="1", text="Шар 1")]
        layer_dropdown.value = "1"
        layer_dropdown.disabled = True
        overlay_checkbox.value = False
        overlay_checkbox.disabled = True

    async def select_profile(profile_id: str) -> None:
        nonlocal client, active_profile, active_profile_id
        if busy:
            return
        if profile_id == active_profile_id and details_panel.visible:
            details_panel.visible = False
            page.update()
            return
        selected = next((profile for profile in profiles if profile.id == profile_id), None)
        if selected is None:
            return
        if active_profile_id != profile_id:
            reset_selected_job()
        active_profile_id = profile_id
        active_profile = selected
        active_profile_heading.value = f"Керування · {selected.name}"
        client = clients_by_profile.get(profile_id)
        details_panel.visible = True
        if client is None:
            error = connection_errors.get(profile_id)
            message_text.value = (
                f"Не вдалося підключитися: {error}"
                if error
                else f"Підключення до {selected.name}…"
                if profile_id in connecting_profiles
                else f"Вибрано {selected.name}. Натисни «Підключити» на картці."
            )
            render_status({})
            render_printer_cards()
            page.update()
            return

        message_text.value = (
            f"Підключено до {selected.name}."
            if client.online
            else f"{selected.name}: очікую на свіжий звіт принтера."
        )
        render_status(client.snapshot().get("print", {}))
        render_printer_cards()
        page.update()
        await refresh_from_client()

    async def connect_profile(profile: PrinterProfile) -> None:
        nonlocal client, active_profile
        if profile.id in clients_by_profile or profile.id in connecting_profiles:
            return
        if not profile.enabled:
            connection_errors[profile.id] = "Профіль вимкнений."
            render_printer_cards()
            page.update()
            return

        connecting_profiles.add(profile.id)
        connection_errors.pop(profile.id, None)
        render_printer_cards()
        page.update()
        new_client = PrinterClient(profile)
        try:
            await asyncio.to_thread(new_client.connect)
            received = await asyncio.to_thread(new_client.wait_for_state, 10.0)
            if not received:
                raise ConnectionError("Звіт принтера не надійшов за 10 секунд.")
            if disposed:
                await asyncio.to_thread(new_client.close)
                return
            clients_by_profile[profile.id] = new_client
            if active_profile_id == profile.id:
                client = new_client
                active_profile = profile
                message_text.value = f"Підключено до {profile.name}."
                await refresh_from_client()
        except (OSError, ValueError, RuntimeError) as exc:
            log.exception("Printer connection failed for profile %s", profile.id)
            connection_errors[profile.id] = str(exc)
            await asyncio.to_thread(new_client.close)
            if active_profile_id == profile.id:
                client = None
                message_text.value = f"Не вдалося підключитися: {exc}"
                render_status({})
        finally:
            connecting_profiles.discard(profile.id)
            if not disposed:
                render_printer_cards()
                page.update()

    async def disconnect_profile(profile: PrinterProfile) -> None:
        nonlocal client
        old_client = clients_by_profile.pop(profile.id, None)
        if old_client is None:
            return
        await asyncio.to_thread(old_client.close)
        connection_errors.pop(profile.id, None)
        if active_profile_id == profile.id:
            client = None
            reset_selected_job()
            message_text.value = f"Відключено від {profile.name}."
            render_status({})
        render_printer_cards()
        page.update()

    async def show_profile_dialog(profile: PrinterProfile | None = None) -> None:
        name_field = ft.TextField(
            label="Назва принтера",
            value=profile.name if profile else "",
            autofocus=True,
        )
        model_field = ft.TextField(
            label="Модель (необов'язково)",
            value=profile.model if profile and profile.model != "UNKNOWN" else "",
            hint_text="Наприклад, A1 mini",
        )
        ip_field = ft.TextField(label="IP-адреса", value=profile.ip if profile else "")
        serial_field = ft.TextField(
            label="Серійний номер (SN)",
            value=profile.serial if profile else "",
        )
        access_code_field = ft.TextField(
            label="Код доступу LAN (8 символів)",
            value=profile.access_code if profile else "",
            password=True,
            can_reveal_password=True,
        )
        enabled_field = ft.Checkbox(
            label="Підключатися автоматично",
            value=profile.enabled if profile else True,
        )
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Редагувати принтер" if profile else "Додати принтер"),
            content=ft.Column(
                [
                    name_field,
                    model_field,
                    ip_field,
                    serial_field,
                    access_code_field,
                    enabled_field,
                    ft.Text(
                        "Для LAN-підключення обов'язкові IP-адреса, SN і код доступу. "
                        "Модель впливає лише на доступність моделі столу та лімітів.",
                        size=12,
                        color=theme_colors["muted"],
                    ),
                    ft.Text(
                        "Закріпи цю IP-адресу за принтером у налаштуваннях DHCP роутера. "
                        "Інакше після перезапуску принтера IP може змінитися.",
                        size=12,
                        color=theme_colors["warning"],
                    ),
                ],
                tight=True,
                scroll=ft.ScrollMode.AUTO,
            ),
            actions_alignment=ft.MainAxisAlignment.END,
        )

        async def cancel(_: Any) -> None:
            page.pop_dialog()

        async def save_profile(_: Any) -> None:
            nonlocal client, active_profile
            try:
                updated = PrinterProfile(
                    name=name_field.value or "",
                    model=(model_field.value or "").strip() or "UNKNOWN",
                    ip=ip_field.value or "",
                    serial=serial_field.value or "",
                    access_code=access_code_field.value or "",
                    enabled=bool(enabled_field.value),
                    id=profile.id if profile else uuid.uuid4().hex,
                )
                updated.validate()
                if any(
                    current.name.casefold() == updated.name.casefold()
                    and current.id != updated.id
                    for current in profiles
                ):
                    raise ValueError("Назва принтера має бути унікальною.")
                next_profiles = profiles.copy()
                if profile is None:
                    next_profiles.append(updated)
                else:
                    index = next(i for i, current in enumerate(next_profiles)
                                 if current.id == profile.id)
                    next_profiles[index] = updated
                save_profiles(next_profiles)
            except (OSError, ValueError, StopIteration) as exc:
                message_text.value = f"Не вдалося зберегти профіль: {exc}"
                page.update()
                return

            page.pop_dialog()
            if profile is not None:
                old_client = clients_by_profile.pop(profile.id, None)
                if old_client is not None:
                    await asyncio.to_thread(old_client.close)
                connection_errors.pop(profile.id, None)
                if active_profile_id == profile.id:
                    client = None
                    active_profile = updated
                    reset_selected_job()
            profiles[:] = next_profiles
            render_printer_cards()
            if profile is None or active_profile_id == updated.id:
                await select_profile(updated.id)
            await connect_profile(updated)
            page.update()

        dialog.actions = [
            ft.TextButton("Скасувати", on_click=cancel),
            ft.FilledButton("Зберегти", on_click=save_profile),
        ]
        page.show_dialog(dialog)

    async def confirm_delete_profile(profile: PrinterProfile) -> None:
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"Видалити «{profile.name}»?"),
            content=ft.Text(
                "Профіль буде видалено з локального JSON-файлу. "
                "Це не змінить налаштування самого принтера."
            ),
            actions_alignment=ft.MainAxisAlignment.END,
        )

        async def cancel(_: Any) -> None:
            page.pop_dialog()

        async def delete(_: Any) -> None:
            nonlocal client, active_profile, active_profile_id
            next_profiles = [item for item in profiles if item.id != profile.id]
            try:
                save_profiles(next_profiles)
            except (OSError, ValueError) as exc:
                message_text.value = f"Не вдалося видалити профіль: {exc}"
                page.update()
                return
            page.pop_dialog()
            old_client = clients_by_profile.pop(profile.id, None)
            if old_client is not None:
                await asyncio.to_thread(old_client.close)
            connection_errors.pop(profile.id, None)
            profiles[:] = next_profiles
            if active_profile_id == profile.id:
                client = None
                active_profile = None
                active_profile_id = None
                details_panel.visible = False
                reset_selected_job()
            render_printer_cards()
            page.update()

        dialog.actions = [
            ft.TextButton("Залишити", on_click=cancel),
            ft.FilledButton(
                "Видалити",
                on_click=delete,
                style=ft.ButtonStyle(
                    bgcolor=theme_colors["cancel_button"],
                    color=theme_colors["cancel_button_text"],
                ),
            ),
        ]
        page.show_dialog(dialog)

    async def refresh_clicked(_: Any) -> None:
        if client is None or busy:
            return
        try:
            await asyncio.to_thread(client.request_full_state)
            await asyncio.to_thread(client.wait_for_state, 5.0)
            await refresh_from_client()
        except (ConnectionError, OSError, RuntimeError) as exc:
            log.exception("Printer refresh failed")
            message_text.value = f"Не вдалося оновити стан: {exc}"
            page.update()

    async def add_printer_clicked(_: Any) -> None:
        await show_profile_dialog()

    async def collapse_details_clicked(_: Any) -> None:
        details_panel.visible = False
        page.update()

    refresh_button.on_click = refresh_clicked
    add_printer_button.on_click = add_printer_clicked
    collapse_details_button.on_click = collapse_details_clicked
    pause_button.on_click = pause_clicked
    resume_button.on_click = resume_clicked
    stop_button.on_click = stop_clicked
    skip_selected_button.on_click = skip_selected_clicked
    layer_dropdown.on_select = layer_changed
    overlay_checkbox.on_change = overlay_changed
    theme_modes = {
        "system": ft.ThemeMode.SYSTEM,
        "light": ft.ThemeMode.LIGHT,
        "dark": ft.ThemeMode.DARK,
    }

    bed_surface = ft.Container(
        content=bed_stack,
        alignment=ft.Alignment.CENTER,
        padding=10,
        bgcolor=theme_colors["surface_alt"],
        border_radius=12,
    )
    bed_card = ft.Container(
        content=ft.Column(
            [
                ft.Text("Площина столу", size=18, weight=ft.FontWeight.BOLD),
                bed_legend,
                ft.Row(
                    [layer_dropdown, overlay_checkbox],
                    alignment=ft.MainAxisAlignment.CENTER,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    wrap=True,
                ),
                bed_surface,
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=8,
        ),
        padding=16,
        bgcolor=theme_colors["surface"],
        border_radius=14,
        border=ft.Border.all(1, theme_colors["border"]),
    )
    details_intro_text = ft.Text(
        "JSON ID і G-code label ID показані окремо.",
        color=theme_colors["muted"],
    )
    details_card = ft.Container(
        content=ft.Column(
            [
                ft.Text("Об'єкти на пластині", size=18, weight=ft.FontWeight.BOLD),
                details_intro_text,
                selection_summary_text,
                selection_hint_text,
                skip_selected_button,
                object_list,
            ],
            spacing=10,
        ),
        padding=16,
        bgcolor=theme_colors["surface"],
        border_radius=14,
        border=ft.Border.all(1, theme_colors["border"]),
    )
    status_card = ft.Container(
        content=ft.Column(
            [
                ft.Row(
                    [
                        state_badge,
                        ft.Container(expand=True),
                        busy_indicator,
                        connection_text,
                        refresh_button,
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    wrap=True,
                ),
                ft.Row(
                    [pause_button, resume_button, stop_button],
                    wrap=True,
                    spacing=8,
                ),
                job_text,
                ft.Row(
                    [progress_percent_text, progress_bar],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=16,
                ),
                progress_text,
                ft.Row([nozzle_text, bed_text], wrap=True, spacing=16),
                skipped_text,
            ],
            spacing=8,
        ),
        padding=16,
        bgcolor=theme_colors["surface"],
        border_radius=14,
        border=ft.Border.all(1, theme_colors["border"]),
    )
    objects_row = ft.ResponsiveRow(
        [
            ft.Container(content=bed_card, col={"sm": 12, "md": 7}),
            ft.Container(content=details_card, col={"sm": 12, "md": 5}),
        ],
        spacing=12,
        run_spacing=12,
        visible=False,
    )
    details_panel.controls = [
        ft.Row(
            [
                active_profile_heading,
                ft.Container(expand=True),
                collapse_details_button,
            ],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            wrap=True,
        ),
        status_card,
        objects_row,
    ]
    title_text = ft.Text("BambuDeck", size=28, weight=ft.FontWeight.BOLD, color=theme_colors["text"])
    subtitle_text = ft.Text(
        "Локальне керування Bambu Lab · друк і об'єкти",
        color=theme_colors["muted"],
    )
    root = ft.Column(
        [
            ft.Row(
                [title_text, theme_dropdown],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                wrap=True,
            ),
            subtitle_text,
            ft.Row(
                [
                    ft.Text(
                        "Принтери",
                        size=20,
                        weight=ft.FontWeight.BOLD,
                        color=theme_colors["text"],
                    ),
                    add_printer_button,
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                wrap=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=10,
            ),
            printer_cards_column,
            message_text,
            details_panel,
        ],
        spacing=14,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
    )

    def apply_theme(mode: ft.ThemeMode) -> None:
        nonlocal theme_mode, effective_theme, theme_colors
        theme_mode = mode
        effective_theme = _effective_theme(mode, page.platform_brightness)
        theme_colors = THEME_PALETTES[effective_theme]
        page.theme_mode = mode
        page.bgcolor = theme_colors["page"]
        title_text.color = theme_colors["text"]
        subtitle_text.color = theme_colors["muted"]
        active_profile_heading.color = theme_colors["text"]
        details_intro_text.color = theme_colors["muted"]
        message_text.color = theme_colors["muted"]
        job_text.color = theme_colors["text"]
        progress_percent_text.color = theme_colors["text"]
        progress_text.color = theme_colors["muted"]
        nozzle_text.color = theme_colors["text"]
        bed_text.color = theme_colors["text"]
        skipped_text.color = theme_colors["muted"]
        selection_summary_text.color = theme_colors["text"]
        selection_hint_text.color = theme_colors["warning"]
        selected_swatch.bgcolor = theme_colors["selected"]
        skipped_swatch.bgcolor = theme_colors["skipped"]
        progress_bar.color = theme_colors["progress"]
        progress_bar.bgcolor = theme_colors["track"]
        pause_button.style = ft.ButtonStyle(
            bgcolor=theme_colors["pause_button"],
            color=theme_colors["pause_button_text"],
        )
        resume_button.style = ft.ButtonStyle(
            bgcolor=theme_colors["resume_button"],
            color=theme_colors["resume_button_text"],
        )
        stop_button.style = ft.ButtonStyle(
            bgcolor=theme_colors["cancel_button"],
            color=theme_colors["cancel_button_text"],
        )
        state_badge.bgcolor = theme_colors["surface_alt"]
        bed_surface.bgcolor = theme_colors["surface_alt"]
        for card in (bed_card, details_card, status_card):
            card.bgcolor = theme_colors["surface"]
            card.border = ft.Border.all(1, theme_colors["border"])
        for control in bed_legend.controls:
            if isinstance(control, ft.Row):
                for child in control.controls:
                    if isinstance(child, ft.Text):
                        child.color = theme_colors["muted"]
            elif isinstance(control, ft.Text):
                control.color = theme_colors["muted"]
        render_status(client.snapshot().get("print", {}) if client else {})
        render_printer_cards()
        page.update()

    async def theme_changed(_: Any) -> None:
        mode = theme_modes.get(theme_dropdown.value or "system", ft.ThemeMode.SYSTEM)
        apply_theme(mode)

    def platform_brightness_changed(_: Any) -> None:
        if theme_mode == ft.ThemeMode.SYSTEM:
            apply_theme(theme_mode)

    def page_resized(_: Any) -> None:
        page.padding = 12 if page.width and page.width < 600 else 20
        render_objects(client.snapshot().get("print", {}) if client else {})
        page.update()

    theme_dropdown.on_select = theme_changed
    page.on_platform_brightness_change = platform_brightness_changed
    page.on_resize = page_resized
    page.add(root)
    render_objects({})
    render_printer_cards()
    page.update()

    async def update_loop() -> None:
        nonlocal disposed
        while not disposed:
            if client is not None and not busy:
                await refresh_from_client()
            render_printer_cards()
            page.update()
            await asyncio.sleep(2)

    async def connect_enabled_profiles() -> None:
        await asyncio.gather(
            *(connect_profile(profile) for profile in profiles if profile.enabled)
        )

    async def on_close(_: Any) -> None:
        nonlocal disposed, client
        disposed = True
        clients = list(clients_by_profile.values())
        clients_by_profile.clear()
        client = None
        if clients:
            await asyncio.gather(
                *(asyncio.to_thread(printer_client.close) for printer_client in clients)
            )
        temporary_directory.cleanup()

    page.on_close = on_close
    page.run_task(update_loop)
    page.run_task(connect_enabled_profiles)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ft.run(app_main)


if __name__ == "__main__":
    main()
