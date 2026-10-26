"""Offline synthetic-contract fixtures, NOT a verified live Rosstat schema."""

import json
from datetime import datetime, timedelta, timezone

import pytest
import requests

from src.data_models.study_area import StudyArea
from src.etl.adapters.official_statistics import OfficialStatisticsAdapter, SEED
from src.storage import SnapshotStore


AREA = StudyArea("Гатчина", {"type": "Polygon", "coordinates": [
    [[30, 59], [30.1, 59], [30.1, 59.1], [30, 59.1], [30, 59]]]},
    jurisdictions=("Ленинградская область",))
INDEX = "https://78.rosstat.gov.ru/opendata/catalog.json"
TABLE = "https://78.rosstat.gov.ru/opendata/municipality.csv"
CATALOG = {"publisher": "Петростат", "license": "CC BY 4.0", "datasets": [
    {"url": TABLE, "schema": "municipal-statistics-v1", "municipality": AREA.name,
     "jurisdiction": AREA.jurisdictions[0], "metric": "население", "period": 2024, "unit": "человек"}]}
CSV = ("municipality,jurisdiction,period,metric,unit,value\n"
       "Гатчина,Ленинградская область,2024,население,человек,90000\n").encode()


class Response:
    def __init__(self, url, content, media="text/html", status=200, headers=None):
        self.url, self.content, self.status_code = url, content, status
        self.headers = {"Content-Type": media, **(headers or {})}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, chunk_size):
        yield self.content


class Session:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def get(self, url, **kwargs):
        assert kwargs == {"timeout": (10, 30), "stream": True, "verify": True,
                          "allow_redirects": False}
        self.calls.append(url)
        result = self.responses[url]
        if isinstance(result, Exception):
            raise result
        return result


def setup(tmp_path, *, catalog=None, table=CSV, homepage=None, catalog_status=200):
    responses = {
        SEED: Response(SEED, homepage if homepage is not None else
                       b'<a href="/opendata/catalog.json">Official catalog</a>'),
        INDEX: Response(INDEX, json.dumps(CATALOG if catalog is None else catalog,
                                          ensure_ascii=False).encode(), "application/json", catalog_status),
        TABLE: Response(TABLE, table, "text/csv"),
    }
    session = Session(responses)
    store = SnapshotStore(tmp_path / "snapshots")
    cache = tmp_path / "cache"
    return OfficialStatisticsAdapter(store, cache, session=session), store, cache, session


def test_automatic_index_table_shape_excluded_without_live_schema_and_cache(tmp_path):
    adapter, store, cache, session = setup(tmp_path)
    result = adapter.discover(AREA)
    assert result.status == "excluded" and result.reason == "unverified_live_schema"
    assert result.statistics == () and result.candidate is None
    assert session.calls == [SEED, INDEX, TABLE]
    assert len(result.snapshot_ids) == 3
    assert all(store.get_snapshot(sid).area_id == AREA.area_id for sid in result.snapshot_ids)
    assert store.read_content(result.snapshot_ids[-1]) == CSV
    assert store.get_snapshot(result.snapshot_ids[-1]).completeness == "unknown"
    assert result.requests[-1]["url"] == TABLE and result.requests[1]["url"] == INDEX
    replay = OfficialStatisticsAdapter(store, cache, offline=True).discover(AREA)
    assert replay.status == "excluded" and replay.reason == "unverified_live_schema"
    assert replay.statistics == () and replay.candidate is None
    assert [item["outcome"] for item in replay.requests] == ["cache_hit"] * 3
    assert adapter.discover(AREA).snapshot_ids == result.snapshot_ids
    assert session.calls == [SEED, INDEX, TABLE]  # online also prefers fresh cache


@pytest.mark.parametrize("change,expected", [
    ({"homepage": b"no links"}, "not_found"),
    ({"homepage": b'<script>window.location="/opendata/catalog.json"</script>'}, "not_found"),
    ({"catalog_status": 403}, "http_403"),
    ({"catalog_status": 429}, "http_429"),
    ({"catalog": {**CATALOG, "license": "unspecified"}}, "unknown_license"),
    ({"catalog": {**CATALOG, "datasets": [{**CATALOG["datasets"][0], "municipality": "Всеволожск"}]}}, "coverage_mismatch"),
    ({"table": b"municipality,period,value\nGatchina,2024,90000\n"}, "unsupported_schema"),
    ({"table": CSV.replace("Гатчина".encode(), "Тихвин".encode())}, "coverage_mismatch"),
])
def test_exclusions(tmp_path, change, expected):
    adapter, store, _, session = setup(tmp_path, **change)
    result = adapter.discover(AREA)
    assert result.status == "excluded" and result.reason == expected
    assert result.candidate is None and not result.statistics
    assert result.requests and result.requests[0]["url"] == SEED
    assert len(session.calls) <= 5
    assert all(store.get_snapshot(sid).area_id == AREA.area_id for sid in result.snapshot_ids)


@pytest.mark.parametrize("exc,expected", [
    (requests.exceptions.SSLError("TLS"), "tls_error"),
    (requests.exceptions.Timeout("slow"), "timeout"),
    (requests.ConnectionError("offline"), "network_error"),
])
def test_network_exclusion(tmp_path, exc, expected):
    adapter, _, _, session = setup(tmp_path)
    session.responses[SEED] = exc
    result = adapter.discover(AREA)
    assert result.reason == expected and result.requests[0]["outcome"] == expected


def test_offline_missing_expired_corrupt(tmp_path):
    adapter, store, cache, _ = setup(tmp_path)
    offline = OfficialStatisticsAdapter(store, cache, offline=True)
    assert offline.discover(AREA).reason == "cache_missing"
    assert adapter.discover(AREA).reason == "unverified_live_schema"
    pointer = next(p for p in cache.glob("*.json") if SEED == store.get_snapshot(json.loads(p.read_text())["snapshot_id"]).url)
    record = json.loads(pointer.read_text())
    checked = datetime.now(timezone.utc) - timedelta(days=2)
    pointer.write_text(json.dumps({**record, "checked_at": checked.isoformat(),
                                   "expires_at": (checked + timedelta(days=1)).isoformat()}))
    assert offline.discover(AREA).reason == "cache_expired"
    pointer.write_text("{broken")
    assert offline.discover(AREA).reason == "cache_corrupt"
    pointer.write_text(json.dumps(record))
    blob = store._path("blobs", store.get_snapshot(record["snapshot_id"]).content_hash, ".bin")
    blob.write_bytes(b"corrupted")
    assert offline.discover(AREA).reason == "cache_corrupt"


def test_cache_future_pointer_and_same_bytes_refreshed_without_changing_snapshot_date(tmp_path):
    adapter, store, cache, session = setup(tmp_path)
    original = adapter.discover(AREA)
    pointer = next(p for p in cache.glob("*.json") if
                   SEED == store.get_snapshot(json.loads(p.read_text())["snapshot_id"]).url)
    record = json.loads(pointer.read_text())
    checked = datetime.now(timezone.utc) + timedelta(days=10)
    pointer.write_text(json.dumps({**record, "checked_at": checked.isoformat(),
                                   "expires_at": (checked + timedelta(days=1)).isoformat()}))
    assert OfficialStatisticsAdapter(store, cache, offline=True).discover(AREA).reason == "cache_corrupt"
    # Expired but well-formed pointer: online refetches and rechecks identical bytes.
    checked = datetime.now(timezone.utc) - timedelta(days=2)
    pointer.write_text(json.dumps({**record, "checked_at": checked.isoformat(),
                                   "expires_at": (checked + timedelta(days=1)).isoformat()}))
    refreshed = adapter.discover(AREA)
    new_record = json.loads(pointer.read_text())
    assert refreshed.reason == "unverified_live_schema"
    assert refreshed.snapshot_ids == original.snapshot_ids
    assert session.calls == [SEED, INDEX, TABLE, SEED]
    assert new_record["snapshot_id"] == record["snapshot_id"]
    assert datetime.fromisoformat(new_record["checked_at"]) > checked
    assert OfficialStatisticsAdapter(store, cache, offline=True).discover(AREA).reason == "unverified_live_schema"


def test_cache_publish_failure_preserves_old_pointer_and_cleans_temp(tmp_path, monkeypatch):
    adapter, store, cache, _ = setup(tmp_path)
    adapter.discover(AREA)
    pointer = next(p for p in cache.glob("*.json") if
                   SEED == store.get_snapshot(json.loads(p.read_text())["snapshot_id"]).url)
    old = json.loads(pointer.read_text())
    checked = datetime.now(timezone.utc) - timedelta(days=2)
    pointer.write_text(json.dumps({**old, "checked_at": checked.isoformat(),
                                   "expires_at": (checked + timedelta(days=1)).isoformat()}))
    before = pointer.read_bytes()

    def fail_replace(*args):
        raise OSError("publication failed")

    monkeypatch.setattr("src.etl.adapters.official_statistics.os.replace", fail_replace)
    with pytest.raises(OSError, match="publication failed"):
        adapter.discover(AREA)
    assert pointer.read_bytes() == before
    assert not list(cache.glob(".pending-*"))


def test_reject_redirect_media_size_and_forged_catalog_link(tmp_path):
    for response, expected in [
        (Response("https://evil.example/", b"", status=200), "unsafe_redirect"),
        (Response(SEED, b"pdf", media="application/pdf"), "unsupported_schema"),
        (Response(SEED, b"x", headers={"Content-Length": "999999999"}), "oversized_response"),
        (Response(SEED, b"x", headers={"Content-Length": "3"}), "truncated_response"),
    ]:
        adapter, _, _, session = setup(tmp_path / expected)
        session.responses[SEED] = response
        assert adapter.discover(AREA).reason == expected
    adapter, _, _, session = setup(tmp_path / "link", homepage=
        b'<a href="https://evil.example/opendata/catalog.json">fake</a>')
    assert adapter.discover(AREA).reason == "not_found"
    assert session.calls == [SEED]
