"""Offline provenance and immutable storage checks."""

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest

from src.data_models import Evidence, KnowledgeStatus, RunManifest
from src.storage import SnapshotStore
from src.storage import snapshots as storage_module


def snapshot(store, **overrides):
    args = dict(source_id="report", area_id="gatchina", url="https://example.org/report",
                content=b"original\x00\xff", parser_version="1", media_type="application/pdf")
    args.update(overrides)
    return store.save_snapshot(**args)


def evidence(saved, **overrides):
    args = dict(evidence_id="count", snapshot_id=saved.snapshot_id, locator="page 1 / table 2",
                unit="visitors", status="observed", value=12)
    args.update(overrides)
    return Evidence(**args)


def run(saved, item, **overrides):
    args = dict(run_id="run1", area_id=saved.area_id, code_version="v1", config_hash="a" * 64,
                started_at=datetime(2025, 1, 1, tzinfo=timezone.utc), snapshot_ids=(saved.snapshot_id,),
                evidence_ids=(item.evidence_id,), status="succeeded")
    args.update(overrides)
    return RunManifest(**args)


def test_roundtrip_idempotency_and_area_namespace(tmp_path):
    store = SnapshotStore(tmp_path)
    first = snapshot(store, retrieved_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
    again = snapshot(store, retrieved_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    other = snapshot(store, area_id="volosovo")
    assert first == again
    assert first.snapshot_id != other.snapshot_id
    assert first.schema_version == 1
    assert store.get_snapshot(first.snapshot_id) == first
    assert store.read_content(first.snapshot_id) == b"original\x00\xff"
    assert json.loads(json.dumps(first.to_dict()))["retrieved_at"] == first.retrieved_at.isoformat()
    item = evidence(first)
    assert store.save_evidence(item) == item
    assert store.save_evidence(item) == item
    assert store.get_evidence(item.evidence_id) == item
    assert item.to_dict()["status"] == "observed"
    manifest = run(first, item)
    store.save_run(manifest)
    store.save_run(manifest)
    assert store.get_run(manifest.run_id) == manifest
    assert json.loads(json.dumps(manifest.to_dict()))["snapshot_ids"] == [first.snapshot_id]


def test_missing_corrupt_and_invalid_paths(tmp_path):
    store = SnapshotStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.read_content("missing")
    with pytest.raises(ValueError):
        store.read_content("../outside")
    saved = snapshot(store)
    blob = tmp_path / "blobs" / (saved.content_hash + ".bin")
    blob.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="Corrupt"):
        store.read_content(saved.snapshot_id)
    with pytest.raises(ValueError, match="Corrupt"):
        snapshot(store)


def test_evidence_validation_and_references(tmp_path):
    store = SnapshotStore(tmp_path)
    saved = snapshot(store)
    item = evidence(saved)
    assert store.save_evidence(item) == item
    with pytest.raises(ValueError, match="conflict"):
        store.save_evidence(replace(item, value=14))
    assert store.save_evidence(evidence(saved, evidence_id="unknown", status="unknown", value=None,
                                         reason="report does not include a count")).value is None
    for changes in (dict(status="unknown", value=0, reason="missing"),
                    dict(status="unknown", value=None),
                    dict(value=float("nan")), dict(value=float("inf")), dict(unit=" "),
                    dict(status="derived"), dict(status="estimated", method="model"),
                    dict(status="estimated", method="model", lower=13, upper=15),
                    dict(lower=10), dict(schema_version=2)):
        with pytest.raises(ValueError):
            evidence(saved, **changes)
    estimate = evidence(saved, evidence_id="estimate", status=KnowledgeStatus.ESTIMATED,
                        method="sample extrapolation", lower=10, upper=15)
    assert store.save_evidence(estimate).status == KnowledgeStatus.ESTIMATED
    with pytest.raises(FileNotFoundError):
        store.save_evidence(evidence(saved, evidence_id="orphan", snapshot_id="absent"))
    with pytest.raises(ValueError):
        evidence(saved, evidence_id="../escape")


def test_run_integrity_immutability_and_versions(tmp_path):
    store = SnapshotStore(tmp_path)
    saved = snapshot(store)
    item = store.save_evidence(evidence(saved))
    manifest = run(saved, item)
    for broken in (run(saved, item, snapshot_ids=("absent",)),
                   run(saved, item, evidence_ids=("absent",)),
                   run(saved, item, snapshot_ids=()),
                   run(saved, item, area_id="different")):
        with pytest.raises((ValueError, FileNotFoundError)):
            store.save_run(broken)
    store.save_run(manifest)
    with pytest.raises(ValueError, match="conflict"):
        store.save_run(replace(manifest, status="failed"))
    with pytest.raises(ValueError, match="version"):
        replace(manifest, schema_version=9)
    record = tmp_path / "snapshots" / (saved.snapshot_id + ".json")
    data = json.loads(record.read_text(encoding="utf-8"))
    data["schema_version"] = 2
    record.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="version"):
        store.read_content(saved.snapshot_id)


def test_timestamps_must_be_aware(tmp_path):
    store = SnapshotStore(tmp_path)
    with pytest.raises(ValueError, match="timezone-aware"):
        snapshot(store, retrieved_at=datetime(2025, 1, 1))
    saved = snapshot(store)
    with pytest.raises(ValueError, match="timezone-aware"):
        run(saved, evidence(saved), started_at=datetime(2025, 1, 1))


@pytest.mark.parametrize("change", [
    {"source_id": "other"}, {"area_id": "other"}, {"url": "https://other.org"},
    {"parser_version": "other"}, {"completeness": "partial"},
])
def test_tampered_snapshot_identity_rejected_on_all_reads(tmp_path, change):
    store = SnapshotStore(tmp_path)
    saved = snapshot(store)
    path = tmp_path / "snapshots" / (saved.snapshot_id + ".json")
    metadata = json.loads(path.read_text(encoding="utf-8"))
    metadata.update(change)
    path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="identity"):
        store.get_snapshot(saved.snapshot_id)
    with pytest.raises(ValueError, match="identity"):
        store.read_content(saved.snapshot_id)
    with pytest.raises(ValueError, match="identity"):
        snapshot(store)


def test_invalid_contract_values_and_duplicate_references(tmp_path):
    store = SnapshotStore(tmp_path)
    with pytest.raises(ValueError, match="completeness"):
        snapshot(store, completeness="invented_state")
    saved = snapshot(store)
    item = evidence(saved)
    for changes in (dict(status="invented_state"), dict(status=True), dict(status=1),
                    dict(config_hash="abc"), dict(config_hash="A" * 64),
                    dict(snapshot_ids=(saved.snapshot_id,) * 2),
                    dict(evidence_ids=(item.evidence_id,) * 2)):
        with pytest.raises(ValueError):
            run(saved, item, **changes)
    with pytest.raises(ValueError):
        evidence(saved, status=False)
    with pytest.raises(ValueError):
        evidence(saved, method=123)


def test_public_getters_validate_corrupt_records_and_references(tmp_path):
    store = SnapshotStore(tmp_path)
    saved = snapshot(store)
    item = store.save_evidence(evidence(saved))
    manifest = run(saved, item)
    store.save_run(manifest)
    evidence_path = tmp_path / "evidence" / "count.json"
    record = json.loads(evidence_path.read_text(encoding="utf-8"))
    record["evidence_id"] = "different"
    evidence_path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="ID mismatch"):
        store.get_evidence("count")
    with pytest.raises(ValueError, match="ID mismatch"):
        store.get_run("run1")
    evidence_path.write_text(json.dumps(item.to_dict()), encoding="utf-8")
    run_path = tmp_path / "runs" / "run1.json"
    record = json.loads(run_path.read_text(encoding="utf-8"))
    record["config_hash"] = "bad"
    run_path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="config_hash"):
        store.get_run("run1")


def test_blob_conflict_prevents_metadata_publication(tmp_path):
    store = SnapshotStore(tmp_path)
    content = b"preexisting"
    digest = hashlib.sha256(content).hexdigest()
    blob = tmp_path / "blobs" / (digest + ".bin")
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="conflict"):
        snapshot(store, content=content)
    assert not list(tmp_path.rglob("*.json"))
    assert blob.read_bytes() == b"corrupt"


def test_failed_link_does_not_leave_partial_record_or_pending_file(tmp_path, monkeypatch):
    store = SnapshotStore(tmp_path)
    def fail_link(src, dst):
        raise OSError("hardlink unavailable")
    monkeypatch.setattr(storage_module.os, "link", fail_link)
    with pytest.raises(OSError, match="hardlink unavailable"):
        snapshot(store)
    assert not list(tmp_path.rglob("*.json"))
    assert not list(tmp_path.rglob(".pending-*"))
    assert not list(tmp_path.rglob("*.bin"))


def test_failed_metadata_link_preserves_blob_but_not_partial_metadata(tmp_path, monkeypatch):
    store = SnapshotStore(tmp_path)
    original_link = storage_module.os.link

    def fail_metadata_link(src, dst):
        if dst.parent.name == "snapshots":
            raise OSError("metadata hardlink unavailable")
        return original_link(src, dst)

    monkeypatch.setattr(storage_module.os, "link", fail_metadata_link)
    with pytest.raises(OSError, match="metadata hardlink unavailable"):
        snapshot(store)
    assert not list(tmp_path.rglob("*.json"))
    assert not list(tmp_path.rglob(".pending-*"))
    assert len(list(tmp_path.rglob("*.bin"))) == 1


def test_preexisting_invalid_snapshot_metadata_is_not_replaced(tmp_path):
    store = SnapshotStore(tmp_path)
    saved = snapshot(store)
    path = tmp_path / "snapshots" / (saved.snapshot_id + ".json")
    corrupted = b'{"schema_version":2}'
    path.write_bytes(corrupted)
    with pytest.raises(ValueError, match="Invalid|version"):
        snapshot(store)
    assert path.read_bytes() == corrupted


def test_file_snapshot_streams_independent_copy_and_verifies_run(tmp_path, monkeypatch):
    store = SnapshotStore(tmp_path / "store")
    source = tmp_path / "sample.pbf"
    content = b"small PBF test bytes" * 4096
    source.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    original_read_bytes = Path.read_bytes

    def forbid_blob_read(path):
        if path == source or path.suffix == ".bin":
            raise AssertionError("PBF read_bytes called")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", forbid_blob_read)
    args = dict(source_id="geofabrik_extract", area_id="gatchina", url="https://example.org/file.pbf",
                file_path=source, parser_version="v1", media_type="application/vnd.osm.pbf",
                completeness="partial", expected_hash=digest, expected_size=len(content))
    saved = store.save_snapshot_file(**args)
    assert store.save_snapshot_file(**args, retrieved_at=datetime(2026, 1, 1, tzinfo=timezone.utc)) == saved
    assert store.verify_content(saved.snapshot_id) == saved
    source.write_bytes(b"changed")
    assert store.verify_content(saved.snapshot_id) == saved
    manifest = RunManifest("pbf_run", "gatchina", "v1", "a" * 64,
                           datetime(2026, 1, 1, tzinfo=timezone.utc), (saved.snapshot_id,), (), "partial")
    store.save_run(manifest)
    assert store.get_run("pbf_run") == manifest
    blob = store.root / "blobs" / (digest + ".bin")
    blob.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="Corrupt"):
        store.get_run("pbf_run")
    source.write_bytes(content)
    with pytest.raises(ValueError, match="Corrupt"):
        store.save_snapshot_file(**args)


@pytest.mark.parametrize("expected", ["hash", "size"])
def test_file_snapshot_rejects_expected_mismatch_before_metadata(tmp_path, expected):
    store = SnapshotStore(tmp_path / "store")
    source = tmp_path / "sample.pbf"
    source.write_bytes(b"PBF")
    kwargs = {"expected_hash": "0" * 64} if expected == "hash" else {"expected_size": 4}
    with pytest.raises(ValueError, match="mismatch"):
        store.save_snapshot_file("geofabrik_extract", "gatchina", "url", source, "v1",
                                 "application/vnd.osm.pbf", **kwargs)
    assert not list(store.root.rglob("*.json"))
    assert not list(store.root.rglob(".pending-*"))
