"""Product operations only use synthetic CLI outputs; no network or real runs."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from src.presentation import operations


COLLECTION = {"stage": "territory_collection", "collection_status": "succeeded",
              "required_spatial_inputs_collected": True, "run_manifest_id": "col1",
              "status": "partial", "statistics_discovery": {"reason": "unavailable"}}
ENRICHMENT = {"stage": "official_registry_enrichment", "status": "partial",
              "source_run_id": "col1", "run_manifest_id": "enr1",
              "reason": "registry unavailable"}


@pytest.fixture
def fake_cli(monkeypatch):
    calls = []

    def run(argv, *, cwd, shell, stdout, stderr, timeout, check):
        calls.append((argv, cwd, shell, timeout, check))
        stdout.write(json.dumps(COLLECTION, ensure_ascii=False).encode("utf-8"))
        stdout.flush()
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(operations.subprocess, "run", run)
    monkeypatch.setattr(operations, "load_run_view", lambda store, run: {
        "run": {"run_id": run}, "enrichment": None})
    return calls


def test_collect_argv_and_logs_preserve_unicode_and_spaces(tmp_path, fake_cli):
    store = tmp_path / "storage with spaces"
    response = operations.collect_territory("Павловск — Гатчина", store, offline=True, timeout=23)
    assert response["ok"] and response["result"] == COLLECTION
    assert response["returncode"] == 0 and response["error"] is None
    assert fake_cli == [([sys.executable, "-X", "utf8", "-m", "scripts.collect_territory",
                         "Павловск — Гатчина", "--store", str(store.resolve()), "--offline"],
                        operations.ROOT, False, 23, False)]
    assert Path(response["stdout_log"]).read_text(encoding="utf-8")
    assert Path(response["stderr_log"]).exists()
    assert not (store / "logs" / "product-ui" / "action.lock").exists()


@pytest.mark.parametrize("territory", ["", "  ", "-bad", "--offline", "a\x00b", "a" * 1001])
def test_invalid_territory_does_not_launch(tmp_path, fake_cli, territory):
    assert not operations.collect_territory(territory, tmp_path)["ok"]
    assert fake_cli == []


def test_enrich_partial_and_parent_provenance(tmp_path, fake_cli, monkeypatch):
    def run(argv, *, stdout, stderr, **kwargs):
        fake_cli.append(argv)
        stdout.write(json.dumps(ENRICHMENT).encode())
        stdout.flush()
        return subprocess.CompletedProcess(argv, 0)

    def view(store, run):
        return {"run": {"run_id": run},
                "enrichment": {"source_run_id": "col1"} if run == "enr1" else None}

    monkeypatch.setattr(operations, "load_run_view", view)
    monkeypatch.setattr(operations.subprocess, "run", run)
    response = operations.enrich_territory("col1", tmp_path, offline=False)
    assert response["ok"] and response["result"]["reason"] == "registry unavailable"
    assert fake_cli[-1] == [sys.executable, "-X", "utf8", "-m", "scripts.enrich_territory",
                            "--run-id", "col1", "--store", str(tmp_path.resolve())]


def test_unavailable_parent_or_bad_id_does_not_launch(tmp_path, fake_cli, monkeypatch):
    assert not operations.enrich_territory("../bad", tmp_path)["ok"]
    monkeypatch.setattr(operations, "load_run_view", lambda store, run: {
        "run": {"run_id": run}, "enrichment": {"source_run_id": "other"}})
    assert "E04 unavailable" in operations.enrich_territory("enr1", tmp_path)["error"]
    assert fake_cli == []


@pytest.mark.parametrize("body,code", [
    (b"", 0), (b"not json", 0), (b"[]", 0), (b'{"a":NaN}', 0),
    (b'{"a":1e999}', 0), (b'{"stage":"bad","stage":"territory_collection"}', 0),
    (b"x" * (operations.OUTPUT_LIMIT + 1), 0),
    (json.dumps(COLLECTION).encode(), 2),
    (json.dumps({**COLLECTION, "required_spatial_inputs_collected": False}).encode(), 0),
], ids=["empty", "malformed", "array", "nonfinite", "overflow", "duplicate-keys", "oversized", "nonzero", "missing-inputs"])
def test_bad_cli_output_is_not_success(tmp_path, fake_cli, monkeypatch, body, code):
    def run(argv, *, stdout, stderr, **kwargs):
        stdout.write(body)
        stdout.flush()
        return subprocess.CompletedProcess(argv, code)

    monkeypatch.setattr(operations.subprocess, "run", run)
    response = operations.collect_territory("Гатчина", tmp_path)
    assert not response["ok"] and response["error"]
    assert response["returncode"] == code
    assert Path(response["stderr_log"]).read_text(encoding="utf-8").strip()


def test_unverified_result_is_not_selected(tmp_path, fake_cli, monkeypatch):
    monkeypatch.setattr(operations, "load_run_view", lambda store, run: {
        "run": {"run_id": "other"}, "enrichment": None})
    assert not operations.collect_territory("Гатчина", tmp_path)["ok"]


def test_timeout_releases_only_own_lock(tmp_path, fake_cli, monkeypatch):
    def timeout(argv, *, stdout, stderr, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(operations.subprocess, "run", timeout)
    response = operations.collect_territory("Гатчина", tmp_path, timeout=1)
    assert not response["ok"] and response["returncode"] is None
    assert not (tmp_path / "logs" / "product-ui" / "action.lock").exists()


def test_foreign_lock_is_never_removed_or_launched(tmp_path, fake_cli):
    lock = tmp_path / "logs" / "product-ui" / "action.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("someone else", encoding="utf-8")
    response = operations.collect_territory("Гатчина", tmp_path)
    assert not response["ok"] and "running" in response["error"]
    assert fake_cli == [] and lock.read_text(encoding="utf-8") == "someone else"
