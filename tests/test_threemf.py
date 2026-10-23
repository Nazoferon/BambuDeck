import json
import zipfile

import pytest

from core.threemf import (
    GcodeObjectLabel,
    ObjectLabelMatch,
    PlateGcodeLabels,
    PlateObject,
    parse_3mf_gcode_labels,
    parse_3mf_objects,
    match_gcode_labels_to_objects,
)


def _write_3mf(path, plates):
    with zipfile.ZipFile(path, "w") as archive:
        for plate_number, metadata in plates.items():
            archive.writestr(
                f"Metadata/plate_{plate_number}.json",
                json.dumps(metadata, ensure_ascii=False),
            )


def _write_gcode(path, files):
    with zipfile.ZipFile(path, "a") as archive:
        for plate_number, gcode in files.items():
            archive.writestr(f"Metadata/plate_{plate_number}.gcode", gcode)


def test_parse_objects_from_plate_metadata(tmp_path):
    archive = tmp_path / "objects.3mf"
    _write_3mf(archive, {
        1: {
            "bbox_objects": [{
                "id": 83,
                "name": "Диск",
                "bbox": [81.000001, 81.000001, 98.999999, 98.999999],
            }],
        },
    })

    assert parse_3mf_objects(archive) == [
        PlateObject(
            plate_number=1,
            id=83,
            name="Диск",
            bbox=(81.000001, 81.000001, 98.999999, 98.999999),
        ),
    ]


def test_parse_objects_from_multiple_plates_in_numeric_order(tmp_path):
    archive = tmp_path / "multiple.3mf"
    _write_3mf(archive, {
        10: {"bbox_objects": [{"id": 2, "name": "ten", "bbox": [0, 0, 1, 1]}]},
        2: {"bbox_objects": [{"id": 1, "name": "two", "bbox": [1, 1, 2, 2]}]},
    })

    assert [(obj.plate_number, obj.id) for obj in parse_3mf_objects(archive)] == [
        (2, 1),
        (10, 2),
    ]


def test_parse_objects_uses_fallback_name(tmp_path):
    archive = tmp_path / "unnamed.3mf"
    _write_3mf(archive, {
        1: {"bbox_objects": [{"id": 7, "name": "", "bbox": [0, 0, 1, 1]}]},
    })

    assert parse_3mf_objects(archive)[0].name == "Object #7"


@pytest.mark.parametrize("bbox", [[0, 1, 2], [0, 1, "2", 3], [0, 1, float("inf"), 3]])
def test_parse_objects_rejects_invalid_bbox(tmp_path, bbox):
    archive = tmp_path / "invalid.3mf"
    _write_3mf(archive, {
        1: {"bbox_objects": [{"id": 1, "name": "bad", "bbox": bbox}]},
    })

    with pytest.raises(ValueError, match="bbox"):
        parse_3mf_objects(archive)


def test_parse_objects_rejects_archive_without_plate_metadata(tmp_path):
    archive = tmp_path / "empty.3mf"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Metadata/other.json", "{}")

    with pytest.raises(ValueError, match="plate_N.json"):
        parse_3mf_objects(archive)


def test_parse_gcode_object_labels_and_preserves_m624_parameters(tmp_path):
    archive = tmp_path / "labeled.3mf"
    _write_3mf(archive, {1: {"bbox_objects": []}})
    _write_gcode(archive, {
        1: """; gcode_label_objects = 1
; start printing object, unique label id: 198
M624 BAAAAAAAAAA=
G1 X10 Y20
M624 BwAAAAAAAAA=
; stop printing object, unique label id: 198
; start printing object, unique label id: 163
M624 AgAAAAAAAAA=
; stop printing object, unique label id: 163
""",
    })

    assert parse_3mf_gcode_labels(archive) == [
        PlateGcodeLabels(
            plate_number=1,
            enabled=True,
            labels=(
                GcodeObjectLabel(198, ("BAAAAAAAAAA=", "BwAAAAAAAAA="), ((10.0, 20.0),)),
                GcodeObjectLabel(163, ("AgAAAAAAAAA=",)),
            ),
        ),
    ]


def test_parse_first_layer_paths_from_extruding_moves_only(tmp_path):
    archive = tmp_path / "first_layer_paths.3mf"
    _write_3mf(archive, {1: {"bbox_objects": []}})
    _write_gcode(archive, {
        1: """; Z_HEIGHT: 0.2
G90
M83
; start printing object, unique label id: 12
G1 X0 Y0 Z0.2 F3000
G1 X10 Y0 E1
G1 X10 Y10 E-0.2
G1 X0 Y10 E1
G1 X0 Y0 Z0.4
G1 X10 Y0 E1
; stop printing object, unique label id: 12
""",
    })

    label = parse_3mf_gcode_labels(archive)[0].labels[0]
    assert label.first_layer_paths == (
        ((0.0, 0.0), (10.0, 0.0)),
        ((10.0, 10.0), (0.0, 10.0)),
    )


def test_parse_first_layer_paths_in_absolute_extrusion_mode(tmp_path):
    archive = tmp_path / "absolute_extrusion.3mf"
    _write_3mf(archive, {1: {"bbox_objects": []}})
    _write_gcode(archive, {
        1: """; Z_HEIGHT: 0.2
G90
M82
G92 E0
; start printing object, unique label id: 6
G1 X1 Y1 Z0.2
G1 X3 Y1 E0.8
G1 X3 Y4 E1.4
; stop printing object, unique label id: 6
""",
    })

    label = parse_3mf_gcode_labels(archive)[0].labels[0]
    assert label.first_layer_paths == (
        ((1.0, 1.0), (3.0, 1.0), (3.0, 4.0)),
    )


def test_parse_paths_for_first_five_layers_only(tmp_path):
    archive = tmp_path / "five_layers.3mf"
    _write_3mf(archive, {1: {"bbox_objects": []}})
    lines = [
        "G90",
        "M83",
        "; start printing object, unique label id: 24",
    ]
    for layer in range(1, 7):
        z = layer * 0.2
        lines.extend([
            f"; Z_HEIGHT: {z:.1f}",
            f"G1 X{layer} Y0 Z{z:.1f}",
            f"G1 X{layer + 1} Y0 E1",
        ])
    lines.append("; stop printing object, unique label id: 24")
    _write_gcode(archive, {1: "\n".join(lines)})

    label = parse_3mf_gcode_labels(archive)[0].labels[0]

    assert len(label.layer_paths) == 5
    assert all(len(layer_paths) == 1 for layer_paths in label.layer_paths)
    assert label.first_layer_paths == label.layer_paths[0]


def test_parse_merges_repeated_object_labels_across_layer_sections(tmp_path):
    archive = tmp_path / "repeated_labels.3mf"
    _write_3mf(archive, {1: {"bbox_objects": []}})
    lines = ["G90", "M83"]
    for layer in range(1, 7):
        z = layer * 0.2
        lines.extend([
            f"; Z_HEIGHT: {z:.1f}",
            f"; start printing object, unique label id: 24",
            f"G1 X{layer} Y0 Z{z:.1f}",
            f"G1 X{layer + 1} Y0 E1",
            f"; stop printing object, unique label id: 24",
        ])
    _write_gcode(archive, {1: "\n".join(lines)})

    labels = parse_3mf_gcode_labels(archive)[0].labels

    assert len(labels) == 1
    assert len(labels[0].layer_paths) == 5
    assert [
        layer_paths[0][0][0]
        for layer_paths in labels[0].layer_paths
    ] == [1.0, 2.0, 3.0, 4.0, 5.0]


def test_parse_gcode_labels_from_multiple_plates_in_numeric_order(tmp_path):
    archive = tmp_path / "multiple.3mf"
    _write_3mf(archive, {2: {"bbox_objects": []}, 10: {"bbox_objects": []}})
    _write_gcode(archive, {
        10: "; gcode_label_objects = 0\n",
        2: "; gcode_label_objects = 1\n",
    })

    assert [(plate.plate_number, plate.enabled) for plate in parse_3mf_gcode_labels(archive)] == [
        (2, True),
        (10, False),
    ]


def test_parse_gcode_labels_does_not_include_metadata_object_ids(tmp_path):
    archive = tmp_path / "distinct_ids.3mf"
    _write_3mf(archive, {
        1: {"bbox_objects": [{"id": 159, "name": "Cube", "bbox": [0, 0, 1, 1]}]},
    })
    _write_gcode(archive, {
        1: "; gcode_label_objects = 1\n; start printing object, unique label id: 198\n"
           "; stop printing object, unique label id: 198\n",
    })

    metadata_id = parse_3mf_objects(archive)[0].id
    label_id = parse_3mf_gcode_labels(archive)[0].labels[0].label_id
    assert metadata_id == 159
    assert label_id == 198


def test_parse_gcode_labels_returns_empty_when_archive_has_no_gcode(tmp_path):
    archive = tmp_path / "without_gcode.3mf"
    _write_3mf(archive, {1: {"bbox_objects": []}})

    assert parse_3mf_gcode_labels(archive) == []


def test_parse_gcode_labels_tracks_relative_and_reset_xy_coordinates(tmp_path):
    archive = tmp_path / "relative_moves.3mf"
    _write_3mf(archive, {1: {"bbox_objects": []}})
    _write_gcode(archive, {
        1: """G90
G1 X10 Y20
G91
; start printing object, unique label id: 5
G1 X1 Y-2
G1 X1 Y1
G92 X0 Y0
G1 X2 Y3
; stop printing object, unique label id: 5
""",
    })

    labels = parse_3mf_gcode_labels(archive)[0].labels
    assert labels[0].xy_points == ((11.0, 18.0), (12.0, 19.0), (2.0, 3.0))


def test_match_gcode_labels_to_objects_by_xy_points(tmp_path):
    archive = tmp_path / "mapped.3mf"
    _write_3mf(archive, {
        1: {"bbox_objects": [
            {"id": 159, "name": "Cube", "bbox": [10, 10, 20, 20]},
            {"id": 192, "name": "Disk", "bbox": [40, 40, 50, 50]},
        ]},
    })
    _write_gcode(archive, {
        1: """; gcode_label_objects = 1
; start printing object, unique label id: 198
G1 X12 Y12
G1 X14 Y16
G1 X19 Y18
; stop printing object, unique label id: 198
; start printing object, unique label id: 163
G1 X42 Y43
G1 X48 Y45
G1 X44 Y49
; stop printing object, unique label id: 163
""",
    })

    matches = match_gcode_labels_to_objects(
        parse_3mf_objects(archive),
        parse_3mf_gcode_labels(archive),
    )
    assert matches == [
        ObjectLabelMatch(1, 159, 198, 3, 3, 1.0),
        ObjectLabelMatch(1, 192, 163, 3, 3, 1.0),
    ]


def test_match_gcode_labels_to_objects_requires_enabled_labels(tmp_path):
    archive = tmp_path / "unlabeled.3mf"
    _write_3mf(archive, {
        1: {"bbox_objects": [{"id": 1, "name": "part", "bbox": [0, 0, 10, 10]}]},
    })
    _write_gcode(archive, {
        1: """; gcode_label_objects = 0
; start printing object, unique label id: 10
G1 X1 Y1
G1 X2 Y2
G1 X3 Y3
; stop printing object, unique label id: 10
""",
    })

    with pytest.raises(ValueError, match="not enabled"):
        match_gcode_labels_to_objects(
            parse_3mf_objects(archive),
            parse_3mf_gcode_labels(archive),
        )


def test_match_gcode_labels_to_objects_rejects_unmatched_object(tmp_path):
    archive = tmp_path / "unmatched.3mf"
    _write_3mf(archive, {
        1: {"bbox_objects": [
            {"id": 1, "name": "part", "bbox": [0, 0, 10, 10]},
            {"id": 2, "name": "missing", "bbox": [30, 30, 40, 40]},
        ]},
    })
    _write_gcode(archive, {
        1: """; gcode_label_objects = 1
; start printing object, unique label id: 10
G1 X1 Y1
G1 X2 Y2
G1 X3 Y3
; stop printing object, unique label id: 10
""",
    })

    with pytest.raises(ValueError, match="Could not confidently match"):
        match_gcode_labels_to_objects(
            parse_3mf_objects(archive),
            parse_3mf_gcode_labels(archive),
        )
