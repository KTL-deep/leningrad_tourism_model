"""Overpass inventory adapter: synthetic responses, no live network or real sleeps."""

from datetime import datetime, timedelta, timezone
import json

import pytest
import requests

from src.data_models.study_area import StudyArea
from src.etl.adapters import overpass_inventory as overpass
from src.storage import SnapshotStore


def rectangle(west=30, south=59, east=30.01, north=59.01):
    return {"type": "Polygon", "coordinates": [[
        [west, south], [east, south], [east, north], [west, north], [west, south],
    ]]}


@pytest.fixture
def area():
    return StudyArea("Small", rectangle())


def payload():
    return {"version": 0.6, "generator": "Overpass API", "osm3s": {
        "timestamp_osm_base": "2026-10-19T00:00:00Z", "copyright": "OpenStreetMap contributors"},
        "elements": [{"type": "node", "id": 1, "lat": 59.005, "lon": 30.005,
                      "tags": {"tourism": "museum"}},
                     {"type": "count", "id": 0, "tags": {"nodes": "1", "ways": "0",
                                                       "relations": "0", "total": "1"}}]}


class Response:
    def __init__(self, body=None, status=200, headers=None):
        self.body = json.dumps(payload() if body is None else body).encode() if not isinstance(body, bytes) else body
        self.status_code = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def iter_content(self, chunk_size):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start:start + chunk_size]


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def clock(monkeypatch):
    state = {"now": datetime(2026, 10, 19, tzinfo=timezone.utc), "sleeps": []}
    monkeypatch.setattr(overpass, "_now", lambda: state["now"])

    def sleep(seconds):
        state["sleeps"].append(seconds)
        state["now"] += timedelta(seconds=seconds)

    monkeypatch.setattr(overpass.time, "sleep", sleep)
    return state


@pytest.fixture
def setup(tmp_path, clock):
    store = SnapshotStore(tmp_path / "store")
    cache = tmp_path / "cache"
    session = Session(Response())
    return overpass.OverpassInventoryAdapter(store, cache, session), session, store, cache


def pointer(cache):
    return next(p for p in cache.glob("*.json") if p.name != "last_request.json")


def test_fetch_cache_offline_and_request_provenance(setup, area):
    adapter, session, store, cache = setup
    first = adapter.collect(area)
    assert not first.from_cache and first.attribution == overpass.ATTRIBUTION
    assert first.query_bbox == (59, 30, 59.01, 30.01)
    assert first.snapshot.source_id == overpass.SOURCE_ID
    assert first.snapshot.area_id == area.area_id
    assert first.snapshot.url.startswith(overpass.ENDPOINT + "#query=")
    assert first.inventory.to_dict()
    args, kwargs = session.calls[0]
    assert args == (overpass.ENDPOINT,)
    assert kwargs["data"]["data"] == overpass.build_query(area)
    assert kwargs["timeout"] == (10, 45)
    assert kwargs["verify"] is True and kwargs["allow_redirects"] is False
    assert kwargs["stream"] is True and kwargs["headers"]["User-Agent"]
    offline = overpass.OverpassInventoryAdapter(store, cache, Session(), offline=True)
    cached = offline.collect(area)
    assert cached.from_cache and cached.snapshot == first.snapshot
    assert cached.inventory.to_dict() == first.inventory.to_dict()
    assert cached.observed_at == first.observed_at


def test_bbox_includes_every_multipolygon_and_analysis_envelope():
    first = rectangle()
    second = rectangle(30.04, 59.04, 30.05, 59.05)
    multi = {"type": "MultiPolygon", "coordinates": [first["coordinates"], second["coordinates"]]}
    area = StudyArea("separated", multi)
    assert overpass.collection_bbox(area) == (59, 30, 59.05, 30.05)
    query = overpass.build_query(area)
    assert "(59.0,30.0,59.05,30.05)" in query
    assert '[timeout:25][maxsize:134217728]' in query
    for tag in ('nwr["tourism"]', 'nwr["historic"]', 'nwr["amenity"',
                'nwr["leisure"', 'nwr["shop"', 'nwr["railway"',
                'nwr["highway"="bus_stop"]', 'nwr["public_transport"', 'way["highway"]'):
        assert tag in query
    assert '(._;>;);\nout body center;\nout count;' in query
    expanded = StudyArea("analysis", first, analysis_geometry=rectangle(29.99, 58.99, 30.02, 59.02))
    assert overpass.collection_bbox(expanded) == (58.99, 29.99, 59.02, 30.02)


def test_large_bbox_rejected_without_network(tmp_path):
    area = StudyArea("large", rectangle(30, 59, 31, 60))
    session = Session()
    adapter = overpass.OverpassInventoryAdapter(SnapshotStore(tmp_path), tmp_path / "cache", session)
    with pytest.raises(overpass.OverpassError, match="bbox_too_large"):
        adapter.collect(area)
    assert not session.calls


def test_expiry_failed_refresh_preserves_pointer(setup, area, clock):
    adapter, session, store, cache = setup
    first = adapter.collect(area)
    path = pointer(cache)
    before = path.read_bytes()
    clock["now"] += timedelta(days=2)
    with pytest.raises(overpass.OverpassError, match="cache_expired"):
        overpass.OverpassInventoryAdapter(store, cache, Session(), offline=True).collect(area)
    session.responses.append(Response({"remark": "runtime error", "elements": []}))
    with pytest.raises(overpass.OverpassError, match="invalid_response"):
        adapter.collect(area)
    assert path.read_bytes() == before
    assert store.get_snapshot(first.snapshot.snapshot_id) == first.snapshot
    assert not (cache / "request.lock").exists()


@pytest.mark.parametrize("tamper", ["pointer", "area", "source", "parser", "url", "blob", "key"])
def test_tampered_cache_fails_closed(setup, area, tamper):
    adapter, session, store, cache = setup
    adapter.collect(area)
    path = pointer(cache)
    record = json.loads(path.read_text(encoding="utf-8"))
    if tamper in ("pointer", "key"):
        if tamper == "pointer":
            path.write_text("garbage", encoding="utf-8")
        else:
            record["key"] = "other"
            path.write_text(json.dumps(record), encoding="utf-8")
    elif tamper == "blob":
        snapshot = store.get_snapshot(record["snapshot_id"])
        (store.root / "blobs" / (snapshot.content_hash + ".bin")).write_bytes(b"bad")
    else:
        snap_path = store.root / "snapshots" / (record["snapshot_id"] + ".json")
        snapshot = json.loads(snap_path.read_text(encoding="utf-8"))
        snapshot[{"area": "area_id", "source": "source_id", "parser": "parser_version",
                  "url": "url"}[tamper]] = "tampered"
        snap_path.write_text(json.dumps(snapshot), encoding="utf-8")
    with pytest.raises(overpass.OverpassError, match="invalid_cache"):
        adapter.collect(area)
    assert len(session.calls) == 1


def test_query_identity_and_invalid_pointer_do_not_fetch(setup, area, monkeypatch):
    adapter, session, _, cache = setup
    adapter.collect(area)
    query = overpass.build_query
    monkeypatch.setattr(overpass, "build_query", lambda selected: query(selected) + "\n")
    with pytest.raises(overpass.OverpassError, match="cache_miss"):
        overpass.OverpassInventoryAdapter(adapter.store, cache, Session(), offline=True).collect(area)
    monkeypatch.setattr(overpass, "build_query", query)
    path = pointer(cache)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["checked_at"] = (overpass._now() + timedelta(days=1)).isoformat()
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(overpass.OverpassError, match="invalid_cache"):
        adapter.collect(area)
    assert len(session.calls) == 1


def test_area_and_endpoint_caches_separate(setup, area):
    adapter, session, store, cache = setup
    adapter.collect(area)
    other = StudyArea("other", rectangle(30.001, 59, 30.01, 59.01))
    session.responses.extend([Response(), Response()])
    adapter.collect(other)
    endpoint = "https://operator.example.org/api/interpreter"
    with pytest.raises(overpass.OverpassError, match="access_not_permitted"):
        overpass.OverpassInventoryAdapter(store, cache, session, endpoint=endpoint).collect(area)
    overpass.OverpassInventoryAdapter(store, cache, session, endpoint=endpoint,
                                      terms_confirmed=True).collect(area)
    assert len(session.calls) == 3
    assert len([p for p in cache.glob("*.json") if p.name != "last_request.json"]) == 3


@pytest.mark.parametrize("response,code,calls", [
    (Response(status=403), "access_denied", 1), (Response(status=429), "rate_limited", 1),
    (Response(status=400), "http_error", 1),
    (Response(status=500), "http_error", 1),
    (Response(status=503), "http_error", 2),
    (requests.exceptions.Timeout(), "timeout", 2),
    (requests.exceptions.SSLError(), "tls_error", 1),
    (Response(headers={"Content-Length": str(overpass.MAX_BYTES + 1)}), "response_too_large", 1),
    (Response(b" " * (overpass.MAX_BYTES + 1)), "response_too_large", 1),
    (Response(b"<html>"), "invalid_response", 1),
    (Response(headers={"Content-Type": "text/html"}), "invalid_response", 1),
])
def test_errors_and_bounded_retries(tmp_path, clock, area, response, code, calls):
    session = Session(*([response] * calls))
    adapter = overpass.OverpassInventoryAdapter(SnapshotStore(tmp_path / "store"),
                                                tmp_path / "cache", session)
    with pytest.raises(overpass.OverpassError) as error:
        adapter.collect(area)
    assert error.value.code == code
    assert len(session.calls) == calls
    assert not (tmp_path / "cache" / "request.lock").exists()
    if code == "http_error":
        assert error.value.details["http_status"] == response.status_code
    if calls == 2:
        assert 2 in clock["sleeps"]


def test_missing_count_rejected_before_storage(tmp_path, clock, area):
    invalid = payload()
    invalid["elements"].pop()
    session = Session(Response(invalid))
    store = SnapshotStore(tmp_path / "store")
    cache = tmp_path / "cache"
    with pytest.raises(overpass.OverpassError, match="invalid_response") as error:
        overpass.OverpassInventoryAdapter(store, cache, session).collect(area)
    assert error.value.details["validation_error"] == "missing_count"
    assert not list(cache.glob("[0-9a-f]*.json"))
    assert not (store.root / "snapshots").exists()


def test_transport_query_and_parser_service_vocabulary_agree(area):
    from src.etl.osm_parsing import _AMENITY
    query = overpass.build_query(area)
    assert all(value in query for values in _AMENITY.values() for value in values)


def test_retry_then_success_and_no_retry_on_bad_count(tmp_path, clock, area):
    invalid = payload()
    invalid["elements"][-1]["tags"]["total"] = "5"
    session = Session(requests.exceptions.Timeout(), Response())
    adapter = overpass.OverpassInventoryAdapter(SnapshotStore(tmp_path / "store"),
                                                tmp_path / "cache", session)
    assert adapter.collect(area).inventory.to_dict()
    assert len(session.calls) == 2 and 2 in clock["sleeps"]
    clock["now"] += timedelta(days=2)
    session.responses.extend([Response(invalid), Response()])
    with pytest.raises(overpass.OverpassError, match="invalid_response"):
        adapter.collect(area)
    assert len(session.calls) == 3
    assert len(session.responses) == 1


def test_lock_rate_state_and_ttl(setup, area, clock):
    adapter, session, store, cache = setup
    cache.mkdir()
    lock = cache / "request.lock"
    lock.write_text("foreign", encoding="utf-8")
    with pytest.raises(overpass.OverpassError, match="request_locked"):
        adapter.collect(area)
    assert lock.read_text(encoding="utf-8") == "foreign" and not session.calls
    lock.unlink()
    first = adapter.collect(area)
    second = StudyArea("next", rectangle(30.001, 59, 30.01, 59.01))
    session.responses.append(Response())
    adapter.collect(second)
    assert any(0 < sleep <= overpass.INTERVAL for sleep in clock["sleeps"])
    (cache / "last_request.json").write_text(json.dumps({"at": (clock["now"] + timedelta(days=1)).isoformat()}))
    session.responses.append(Response())
    with pytest.raises(overpass.OverpassError, match="invalid_rate_state"):
        adapter.collect(StudyArea("third", rectangle(30, 59, 30.009, 59.009)))
    assert len(session.calls) == 2
    assert not lock.exists() and first.snapshot
    clock["now"] += timedelta(seconds=2)
    with pytest.raises(overpass.OverpassError, match="cache_expired"):
        overpass.OverpassInventoryAdapter(store, cache, Session(), ttl_seconds=1,
                                          offline=True).collect(area)


@pytest.mark.parametrize("url", ["http://host/api/interpreter", "https://user@host/api/interpreter",
                                      "https://host/api/interpreter?q=x", "https://host/api/interpreter#x",
                                      "https://host/other", "https://host:99999/api/interpreter"])
def test_invalid_endpoint(tmp_path, url):
    with pytest.raises(ValueError, match="endpoint"):
        overpass.OverpassInventoryAdapter(SnapshotStore(tmp_path), tmp_path, endpoint=url)
