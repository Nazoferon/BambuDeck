"""Read plate object metadata from a Bambu/Orca 3MF archive."""
from __future__ import annotations

import json
import math
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PLATE_METADATA = re.compile(r"^Metadata/plate_(\d+)\.json$")
_PLATE_GCODE = re.compile(r"^Metadata/plate_(\d+)\.gcode$")
_LABELS_ENABLED = re.compile(r"^\s*;\s*gcode_label_objects\s*=\s*([01])\s*$", re.IGNORECASE)
_LABEL_START = re.compile(
    r"^\s*;\s*start printing object,\s*unique label id:\s*(\d+)\s*$",
    re.IGNORECASE,
)
_LABEL_STOP = re.compile(
    r"^\s*;\s*stop printing object,\s*unique label id:\s*(\d+)\s*$",
    re.IGNORECASE,
)
_LAYER_Z_HEIGHT = re.compile(r"^\s*;\s*Z_HEIGHT:\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*$", re.IGNORECASE)
_M624 = re.compile(r"^\s*M624(?:\s+([^;]+?))?\s*(?:;.*)?$", re.IGNORECASE)
_GCODE_COMMAND = re.compile(r"^\s*([GMT]\d+(?:\.\d+)?)\b", re.IGNORECASE)
_GCODE_WORD = re.compile(r"\b([A-Z])([-+]?(?:\d+(?:\.\d*)?|\.\d+))\b", re.IGNORECASE)


@dataclass(frozen=True)
class PlateObject:
    plate_number: int
    id: int
    name: str
    bbox: tuple[float, float, float, float]


@dataclass(frozen=True)
class GcodeObjectLabel:
    label_id: int
    m624_parameters: tuple[str, ...]
    xy_points: tuple[tuple[float, float], ...] = ()
    layer_paths: tuple[tuple[tuple[tuple[float, float], ...], ...], ...] = ()

    @property
    def first_layer_paths(self) -> tuple[tuple[tuple[float, float], ...], ...]:
        return self.layer_paths[0] if self.layer_paths else ()


@dataclass(frozen=True)
class PlateGcodeLabels:
    plate_number: int
    enabled: bool
    labels: tuple[GcodeObjectLabel, ...]


@dataclass(frozen=True)
class ObjectLabelMatch:
    plate_number: int
    object_id: int
    label_id: int
    matched_points: int
    total_points: int
    match_fraction: float


def _parse_bbox(value: Any, metadata_name: str, object_id: int) -> tuple[float, float, float, float]:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError(f"{metadata_name}: object {object_id} has an invalid bbox")

    coordinates: list[float] = []
    for coordinate in value:
        if isinstance(coordinate, bool) or not isinstance(coordinate, (int, float)):
            raise ValueError(f"{metadata_name}: object {object_id} has a non-numeric bbox")
        if not math.isfinite(coordinate):
            raise ValueError(f"{metadata_name}: object {object_id} has a non-finite bbox")
        coordinates.append(float(coordinate))
    return coordinates[0], coordinates[1], coordinates[2], coordinates[3]


def _parse_gcode_labels(plate_number: int, gcode: str) -> PlateGcodeLabels:
    enabled = False
    labels: list[GcodeObjectLabel] = []
    active_label_id: int | None = None
    active_parameters: list[str] = []
    active_points: list[tuple[float, float]] = []
    position = {"X": 0.0, "Y": 0.0, "Z": 0.0}
    absolute_positioning = True
    absolute_extrusion = False
    extrusion_position = 0.0
    current_layer_index = -1
    current_layer_z: float | None = None
    active_paths_by_layer: list[list[tuple[tuple[float, float], ...]]] = []
    active_path: list[tuple[float, float]] = []

    def finish_path() -> None:
        if len(active_path) > 1:
            if 0 <= current_layer_index < 5:
                active_paths_by_layer[current_layer_index].append(tuple(active_path))
        active_path.clear()

    def finish_label() -> None:
        assert active_label_id is not None
        finish_path()
        labels.append(GcodeObjectLabel(
            label_id=active_label_id,
            m624_parameters=tuple(active_parameters),
            xy_points=tuple(active_points),
            layer_paths=tuple(tuple(paths) for paths in active_paths_by_layer),
        ))

    for line in gcode.splitlines():
        z_height_match = _LAYER_Z_HEIGHT.match(line)
        if z_height_match:
            finish_path()
            current_layer_index += 1
            current_layer_z = float(z_height_match.group(1))
            if active_label_id is not None and current_layer_index < 5:
                while len(active_paths_by_layer) <= current_layer_index:
                    active_paths_by_layer.append([])

        enabled_match = _LABELS_ENABLED.match(line)
        if enabled_match:
            enabled = enabled_match.group(1) == "1"
            continue

        start_match = _LABEL_START.match(line)
        if start_match:
            if active_label_id is not None:
                finish_label()
            active_label_id = int(start_match.group(1))
            active_parameters = []
            active_points = []
            active_paths_by_layer = [
                [] for _ in range(min(current_layer_index + 1, 5))
            ]
            active_path = []
            continue

        stop_match = _LABEL_STOP.match(line)
        if stop_match:
            if active_label_id == int(stop_match.group(1)):
                finish_label()
                active_label_id = None
                active_parameters = []
                active_points = []
            continue

        if line.lstrip().startswith(";"):
            continue
        command_line = line.split(";", 1)[0].strip()
        command_match = _GCODE_COMMAND.match(command_line)
        if not command_match:
            continue

        command = command_match.group(1).upper()
        if command == "G90":
            absolute_positioning = True
            continue
        if command == "G91":
            absolute_positioning = False
            continue
        if command == "M82":
            absolute_extrusion = True
            continue
        if command == "M83":
            absolute_extrusion = False
            continue
        if active_label_id is not None and command == "M624":
            m624_match = _M624.match(line)
            if m624_match and m624_match.group(1):
                active_parameters.extend(m624_match.group(1).split())
            continue

        words = {
            axis.upper(): float(value)
            for axis, value in _GCODE_WORD.findall(command_line)
        }
        if command == "G92":
            for axis in ("X", "Y", "Z"):
                if axis in words:
                    position[axis] = words[axis]
            if "E" in words:
                extrusion_position = words["E"]
            continue

        if command not in {"G0", "G1", "G2", "G3"}:
            continue

        start_position = position.copy()
        has_xy = "X" in words or "Y" in words
        for axis in ("X", "Y", "Z"):
            if axis in words:
                if absolute_positioning:
                    position[axis] = words[axis]
                else:
                    position[axis] += words[axis]

        extrusion_delta = 0.0
        if "E" in words:
            if absolute_extrusion:
                extrusion_delta = words["E"] - extrusion_position
                extrusion_position = words["E"]
            else:
                extrusion_delta = words["E"]
                extrusion_position += words["E"]

        if active_label_id is not None and has_xy:
            active_points.append((position["X"], position["Y"]))
            is_first_layer = (
                current_layer_z is not None
                and 0 <= current_layer_index < 5
                and abs(position["Z"] - current_layer_z) <= 0.03
                and abs(start_position["Z"] - current_layer_z) <= 0.03
            )
            if extrusion_delta > 0 and is_first_layer:
                segment: list[tuple[float, float]]
                if command in {"G2", "G3"} and ("I" in words or "J" in words):
                    center_x = start_position["X"] + words.get("I", 0.0)
                    center_y = start_position["Y"] + words.get("J", 0.0)
                    start_angle = math.atan2(
                        start_position["Y"] - center_y,
                        start_position["X"] - center_x,
                    )
                    end_angle = math.atan2(
                        position["Y"] - center_y,
                        position["X"] - center_x,
                    )
                    sweep = end_angle - start_angle
                    if command == "G2" and sweep >= 0:
                        sweep -= 2 * math.pi
                    elif command == "G3" and sweep <= 0:
                        sweep += 2 * math.pi
                    radius = math.hypot(
                        start_position["X"] - center_x,
                        start_position["Y"] - center_y,
                    )
                    steps = max(2, min(128, math.ceil(abs(sweep) / (math.pi / 18))))
                    segment = [
                        (
                            center_x + radius * math.cos(start_angle + sweep * step / steps),
                            center_y + radius * math.sin(start_angle + sweep * step / steps),
                        )
                        for step in range(steps + 1)
                    ]
                else:
                    segment = [
                        (start_position["X"], start_position["Y"]),
                        (position["X"], position["Y"]),
                    ]

                if active_path and math.dist(active_path[-1], segment[0]) <= 0.01:
                    active_path.extend(segment[1:])
                else:
                    finish_path()
                    active_path.extend(segment)
            else:
                finish_path()
        elif active_label_id is not None and position["Z"] != start_position["Z"]:
            finish_path()

    if active_label_id is not None:
        finish_label()

    fragments_by_label: dict[int, list[GcodeObjectLabel]] = {}
    for label in labels:
        fragments_by_label.setdefault(label.label_id, []).append(label)
    merged_labels: list[GcodeObjectLabel] = []
    for label_id, fragments in fragments_by_label.items():
        layer_paths: list[list[tuple[tuple[float, float], ...]]] = [
            [] for _ in range(max((len(fragment.layer_paths) for fragment in fragments), default=0))
        ]
        for fragment in fragments:
            for layer_index, paths in enumerate(fragment.layer_paths[:5]):
                layer_paths[layer_index].extend(paths)
        merged_labels.append(GcodeObjectLabel(
            label_id=label_id,
            m624_parameters=tuple(
                parameter
                for fragment in fragments
                for parameter in fragment.m624_parameters
            ),
            xy_points=tuple(
                point
                for fragment in fragments
                for point in fragment.xy_points
            ),
            layer_paths=tuple(tuple(paths) for paths in layer_paths),
        ))

    return PlateGcodeLabels(
        plate_number=plate_number,
        enabled=enabled,
        labels=tuple(merged_labels),
    )


def parse_3mf_objects(path: Path) -> list[PlateObject]:
    """Return objects from all Metadata/plate_N.json entries in a 3MF archive."""
    objects: list[PlateObject] = []

    with zipfile.ZipFile(path) as archive:
        metadata_files = sorted(
            (match for name in archive.namelist() if (match := _PLATE_METADATA.fullmatch(name))),
            key=lambda match: int(match.group(1)),
        )
        if not metadata_files:
            raise ValueError(f"{path} contains no Metadata/plate_N.json files")

        for match in metadata_files:
            metadata_name = match.group(0)
            plate_number = int(match.group(1))
            try:
                metadata = json.loads(archive.read(metadata_name))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{metadata_name} is not valid JSON") from exc

            if not isinstance(metadata, dict):
                raise ValueError(f"{metadata_name} must contain a JSON object")
            entries = metadata.get("bbox_objects")
            if not isinstance(entries, list):
                raise ValueError(f"{metadata_name} has no valid bbox_objects list")

            for entry in entries:
                if not isinstance(entry, dict):
                    raise ValueError(f"{metadata_name} contains an invalid object entry")
                object_id = entry.get("id")
                if isinstance(object_id, bool) or not isinstance(object_id, int):
                    raise ValueError(f"{metadata_name} contains an object with an invalid id")
                name = entry.get("name", "")
                if not isinstance(name, str):
                    raise ValueError(f"{metadata_name}: object {object_id} has an invalid name")
                objects.append(PlateObject(
                    plate_number=plate_number,
                    id=object_id,
                    name=name or f"Object #{object_id}",
                    bbox=_parse_bbox(entry.get("bbox"), metadata_name, object_id),
                ))

    return objects


def parse_3mf_gcode_labels(path: Path) -> list[PlateGcodeLabels]:
    """Read per-object G-code labels without treating them as plate metadata IDs."""
    plates: list[PlateGcodeLabels] = []

    with zipfile.ZipFile(path) as archive:
        gcode_files = sorted(
            (match for name in archive.namelist() if (match := _PLATE_GCODE.fullmatch(name))),
            key=lambda match: int(match.group(1)),
        )
        for match in gcode_files:
            plate_number = int(match.group(1))
            gcode = archive.read(match.group(0)).decode("utf-8-sig", errors="replace")
            plates.append(_parse_gcode_labels(plate_number, gcode))

    return plates


def match_gcode_labels_to_objects(
    objects: list[PlateObject],
    plates: list[PlateGcodeLabels],
    *,
    tolerance_mm: float = 1.0,
    min_fraction: float = 0.8,
    min_points: int = 3,
) -> list[ObjectLabelMatch]:
    """Match labels to plate objects by the fraction of their G-code XY points in each bbox.

    This is a geometric inference from sliced G-code, not proof of the printer's
    skip_objects protocol or a guarantee that the inferred label is accepted by it.
    """
    if not math.isfinite(tolerance_mm) or tolerance_mm < 0:
        raise ValueError("tolerance_mm must be a finite non-negative number")
    if not math.isfinite(min_fraction) or not 0 < min_fraction <= 1:
        raise ValueError("min_fraction must be greater than 0 and at most 1")
    if isinstance(min_points, bool) or not isinstance(min_points, int) or min_points < 1:
        raise ValueError("min_points must be a positive integer")

    objects_by_plate: dict[int, list[PlateObject]] = {}
    for obj in objects:
        objects_by_plate.setdefault(obj.plate_number, []).append(obj)

    matches: list[ObjectLabelMatch] = []
    labels_by_plate = {plate.plate_number: plate for plate in plates}
    for plate_number, plate_objects in objects_by_plate.items():
        plate_labels = labels_by_plate.get(plate_number)
        if plate_labels is None or not plate_labels.enabled:
            raise ValueError(f"Object labels are not enabled for plate {plate_number}")

        for label in plate_labels.labels:
            if len(label.xy_points) < min_points:
                continue

            scored_objects: list[tuple[PlateObject, int, float]] = []
            for obj in plate_objects:
                x_min, y_min, x_max, y_max = obj.bbox
                in_bbox = sum(
                    x_min - tolerance_mm <= x <= x_max + tolerance_mm
                    and y_min - tolerance_mm <= y <= y_max + tolerance_mm
                    for x, y in label.xy_points
                )
                fraction = in_bbox / len(label.xy_points)
                if in_bbox >= min_points and fraction >= min_fraction:
                    scored_objects.append((obj, in_bbox, fraction))

            if len(scored_objects) > 1:
                raise ValueError(
                    f"G-code label {label.label_id} ambiguously matches multiple objects "
                    f"on plate {plate_number}"
                )
            if scored_objects:
                obj, in_bbox, fraction = scored_objects[0]
                matches.append(ObjectLabelMatch(
                    plate_number=plate_number,
                    object_id=obj.id,
                    label_id=label.label_id,
                    matched_points=in_bbox,
                    total_points=len(label.xy_points),
                    match_fraction=fraction,
                ))

        matched_object_ids = {
            match.object_id for match in matches if match.plate_number == plate_number
        }
        missing_ids = {obj.id for obj in plate_objects} - matched_object_ids
        if missing_ids:
            raise ValueError(
                f"Could not confidently match G-code labels for objects {sorted(missing_ids)} "
                f"on plate {plate_number}"
            )

    return matches
