"""Synthetic geometry tests; no boundary provenance is inferred from shapes."""

from dataclasses import FrozenInstanceError
import json

import pytest

from src.data_models import StudyArea


SQUARE = {"type": "Polygon", "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]]}
OUTER = {"type": "Polygon", "coordinates": [[[-1, -1], [3, -1], [3, 3], [-1, 3], [-1, -1]]]}
HOLE = {"type": "Polygon", "coordinates": [
    [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]],
    [[1, 1], [1, 2], [2, 2], [2, 1], [1, 1]],
]}
MULTI = {"type": "MultiPolygon", "coordinates": [
    SQUARE["coordinates"], [[[5, 5], [6, 5], [6, 6], [5, 6], [5, 5]]],
]}


def test_identity_ignores_name_and_geometry_order_but_tracks_scope():
    # Equivalent geometry with a different starting vertex and winding direction.
    reversed_ring = {"type": "Polygon", "coordinates": [[[2, 2], [2, 0], [0, 0], [0, 2], [2, 2]]]}
    area = StudyArea("First", SQUARE)
    assert area.area_id == StudyArea("Alias", reversed_ring).area_id
    assert area.area_id != StudyArea("First", SQUARE, jurisdictions=("District",)).area_id
    assert area.area_id != StudyArea("First", SQUARE, analysis_geometry=OUTER).area_id
    assert StudyArea("First", MULTI).area_id == StudyArea(
        "Alias", {"type": "MultiPolygon", "coordinates": list(reversed(MULTI["coordinates"]))}
    ).area_id
    assert area.to_dict()["surrounding_status"] == "not_configured"
    assert area.analysis_geometry is None
    assert len(area.area_id) == 64


def test_holes_multi_coverage_and_explicit_surroundings():
    for geom in (HOLE, MULTI):
        assert StudyArea("test", geom).request_geometry["type"] == geom["type"]
    area = StudyArea("test", SQUARE, analysis_geometry=OUTER, metric_crs="EPSG:32631")
    assert area.to_dict()["surrounding_status"] == "explicit"
    assert area.metric_crs == "EPSG:32631"
    with pytest.raises(ValueError, match="cover"):
        StudyArea("test", OUTER, analysis_geometry=SQUARE)
    with pytest.raises(ValueError, match="cover"):
        StudyArea("test", HOLE, analysis_geometry=SQUARE)


def test_defensive_copies_frozen_fields_and_json_serialization():
    input_geom = json.loads(json.dumps(SQUARE))
    area = StudyArea("test", input_geom, analysis_geometry=OUTER)
    original = area.to_dict()
    input_geom["coordinates"][0][0][0] = 99
    area.request_geometry["coordinates"][0][0][0] = 99
    area.analysis_geometry["coordinates"][0][0][0] = 99
    assert area.to_dict() == original
    assert json.loads(json.dumps(original)) == original
    with pytest.raises(FrozenInstanceError):
        area.name = "modified"
    with pytest.raises((AttributeError, TypeError)):
        area.request_geometry = SQUARE


@pytest.mark.parametrize("geometry", [
    {"type": "Point", "coordinates": [0, 0]},
    {"type": "Polygon", "coordinates": []},
    {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [0, 0]]]},
    {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1]]]},
    {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [0, 1], [1, 0], [0, 0]]]},
    {"type": "Polygon", "coordinates": [[[0, 0, 1], [1, 0], [1, 1], [0, 0]]]},
    {"type": "Polygon", "coordinates": [[[False, 0], [1, 0], [1, 1], [False, 0]]]},
    {"type": "Polygon", "coordinates": [[[float("nan"), 0], [1, 0], [1, 1], [float("nan"), 0]]]},
    {"type": "Polygon", "coordinates": [[[181, 0], [1, 0], [1, 1], [181, 0]]]},
    {"type": "Polygon", "coordinates": [[[0, 91], [1, 0], [1, 1], [0, 91]]]},
    {"type": "MultiPolygon", "coordinates": [SQUARE["coordinates"], []]},
    {"type": "Polygon", "coordinates": [[0, 0], [1, 0], [1, 1], [0, 0]]},
])
def test_rejects_invalid_geometry(geometry):
    with pytest.raises(ValueError):
        StudyArea("test", geometry)


@pytest.mark.parametrize("kwargs", [
    {"crs": "EPSG:3857"}, {"crs": "missing"},
    {"metric_crs": "EPSG:4326"}, {"metric_crs": "EPSG:2263"},
    {"jurisdictions": ["District"]}, {"jurisdictions": ("",)},
    {"jurisdictions": ("District", "District")},
    {"jurisdiction_status": "verified"},
    {"jurisdiction_status": "invalid"},
])
def test_rejects_invalid_crs_or_jurisdiction(kwargs):
    with pytest.raises(ValueError):
        StudyArea("test", SQUARE, **kwargs)


def test_jurisdiction_status_is_caller_claim_not_inferred():
    area = StudyArea("test", SQUARE, jurisdictions=("District",), jurisdiction_status="verified")
    assert area.jurisdiction_status == "verified"
    assert area.jurisdictions == ("District",)
    assert area.to_dict()["jurisdictions"] == ["District"]


def test_versioned_record_roundtrip_and_identity_validation():
    area = StudyArea("test", SQUARE, analysis_geometry=OUTER)
    assert StudyArea.from_dict(json.loads(json.dumps(area.to_dict()))) == area
    for changes in ({"schema_version": 2}, {"schema_version": True}, {"area_id": "forged"},
                    {"surrounding_status": "not_configured"}, {"jurisdictions": "District"}):
        with pytest.raises(ValueError):
            StudyArea.from_dict({**area.to_dict(), **changes})


@pytest.mark.parametrize("metric_crs", ["invalid_crs", "", 3857, True])
def test_rejects_invalid_metric_crs_types(metric_crs):
    with pytest.raises(ValueError):
        StudyArea("test", SQUARE, metric_crs=metric_crs)


def test_conflicting_embedded_crs_is_not_silently_reinterpreted():
    with pytest.raises(ValueError, match="embedded CRS"):
        StudyArea("test", {**SQUARE, "crs": {"name": "EPSG:3857"}})
