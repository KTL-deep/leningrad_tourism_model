"""Offline CLI integration; synthetic boundary, no live geocoder requests."""

from datetime import datetime, timezone
import json
from types import SimpleNamespace

from scripts import collect_boundary
from src.data_models import StudyArea
from src.etl.adapters.nominatim_boundary import BoundaryError
from src.storage import SnapshotStore


SQUARE = {"type": "Polygon", "coordinates": [[[30, 59], [30.01, 59], [30.01, 59.01], [30, 59.01], [30, 59]]]}


class SyntheticAdapter:
    def __init__(self, store):
        self.store = store

    def collect(self, query):
        area = StudyArea(query, SQUARE)
        observed_at = datetime(2026, 10, 19, tzinfo=timezone.utc)
        snapshot = self.store.save_snapshot(
            "osm_nominatim", area.area_id, "https://example.org/synthetic-geocoder",
            b'[{"synthetic":true}]', "synthetic-adapter-v1", "application/json", "complete", observed_at,
        )
        return SimpleNamespace(study_area=area, snapshot=snapshot, from_cache=False,
                               observed_at=observed_at, attribution="Synthetic fixture, not empirical data")


def test_cli_collects_boundary_but_does_not_claim_analysis(tmp_path, capsys):
    store = SnapshotStore(tmp_path)
    result = collect_boundary.main(["Synthetic Region", "--store", str(tmp_path)], adapter=SyntheticAdapter(store))
    report = json.loads(capsys.readouterr().out)
    assert result == 0
    assert report["status"] == "partial" and report["boundary_status"] == "succeeded"
    assert report["analysis_performed"] is False
    assert "request_geometry" not in report["study_area"]
    assert report["study_area"]["surrounding_status"] == "not_configured"
    assert report["source"]["source_id"] == "osm_nominatim"
    assert report["input_assessment"]["manifest"]["status"] == "failed"  # POI/network not yet collected.
    assert report["input_assessment"]["computed_tourism_results"] == []
    manifest = store.get_run(report["input_assessment"]["manifest"]["run_id"])
    assert manifest.code_version.startswith("sha256:")
    assert report["source"]["snapshot_id"] in manifest.snapshot_ids


def test_cli_failure_is_explicit_and_does_not_create_fake_boundary(tmp_path, capsys):
    class MissingAdapter:
        def collect(self, query):
            raise BoundaryError("not_found")

    result = collect_boundary.main(["Synthetic missing region", "--store", str(tmp_path)], adapter=MissingAdapter())
    report = json.loads(capsys.readouterr().out)
    assert result == 2
    assert report["status"] == "failed" and report["error"] == "not_found"
    assert report["analysis_performed"] is False
    assert "study_area" not in report
    assert not (tmp_path / "snapshots").exists()


def test_bad_operator_endpoint_fails_before_network(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("NOMINATIM_ENDPOINT", "http://example.org/search")
    assert collect_boundary.main(["Synthetic Region", "--store", str(tmp_path)]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["error"] == "local_state_or_configuration_error"
    assert report["analysis_performed"] is False
