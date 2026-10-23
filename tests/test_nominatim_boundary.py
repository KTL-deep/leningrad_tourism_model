"""Nominatim adapter: synthetic fixtures, no external requests or real waiting."""

from datetime import datetime, timedelta, timezone
import json

import pytest
import requests

from src.etl.adapters import nominatim_boundary as boundary
from src.storage import SnapshotStore


POLYGON = {"type": "Polygon", "coordinates": [[[30, 59], [31, 59], [31, 60], [30, 59]]]}


def record(osm_id=123, **changes):
    result = {"osm_type": "relation", "osm_id": osm_id, "category": "boundary",
              "type": "administrative", "display_name": "Павловск", "address": {"state": "Санкт-Петербург"},
              "geojson": POLYGON}
    result.update(changes)
    return result


class Response:
    def __init__(self, records=None, *, content=None, status=200, headers=None):
        self.body = content if content is not None else json.dumps(records).encode("utf-8")
        self.status_code = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def iter_content(self, chunk_size):
        for index in range(0, len(self.body), chunk_size):
            yield self.body[index:index + chunk_size]


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.fixture
def clock(monkeypatch):
    state = {"now": datetime(2026, 10, 19, tzinfo=timezone.utc)}
    monkeypatch.setattr(boundary, "_now", lambda: state["now"])
    monkeypatch.setattr(boundary.time, "sleep", lambda seconds: state.update(now=state["now"] + timedelta(seconds=seconds)))
    return state


@pytest.fixture
def setup(tmp_path, clock):
    session = Session(Response([record()]))
    cache = tmp_path / "cache"
    store = SnapshotStore(tmp_path / "store")
    return boundary.NominatimBoundaryAdapter(store, cache, session), session, cache, store


def pointer(cache):
    return next(p for p in cache.glob("*.json") if p.name != "last_request.json")


def test_station_discarded_cache_reparsed_and_metadata(setup):
    adapter, session, cache, store = setup
    session.response = Response([record(11, category="railway", type="station",
                                        geojson={"type": "Point", "coordinates": [30, 59]}), record()])
    first = adapter.collect("  Павловск   Санкт-Петербург  ")
    assert first.study_area.name == "Павловск"
    assert first.study_area.jurisdictions == ("Санкт-Петербург",)
    assert first.study_area.jurisdiction_status == "source_reported"
    assert first.study_area.analysis_geometry is None
    assert first.attribution == boundary.ATTRIBUTION
    assert first.snapshot.area_id == first.study_area.area_id
    assert first.snapshot.source_id == "osm_nominatim"
    assert first.snapshot.url.startswith(boundary.ENDPOINT + "?q=")
    assert first.observed_at.tzinfo is not None
    assert session.calls[0][1]["params"]["q"] == "павловск санкт-петербург"
    assert session.calls[0][1]["verify"] is True
    assert session.calls[0][1]["timeout"] == (10, 25)
    offline = boundary.NominatimBoundaryAdapter(store, cache, Session(AssertionError("network")), offline=True)
    second = offline.collect("ПАВЛОВСК Санкт-Петербург")
    assert second.from_cache and second.snapshot == first.snapshot
    assert second.study_area.area_id == first.study_area.area_id
    assert second.observed_at == first.observed_at
    assert len(session.calls) == 1


@pytest.mark.parametrize("records,code", [
    ([record(1), record(2)], "ambiguous_boundary"),
    ([record(1, geojson={"type": "Point", "coordinates": [30, 59]})], "not_found"),
    ([record(1, geojson={"type": "Polygon", "coordinates": [[[30, 59], [31, 59], [30, 60]]]})], "invalid_geometry"),
    ({"error": "blocked"}, "invalid_response"),
])
def test_selection_fails_closed(tmp_path, records, code):
    adapter = boundary.NominatimBoundaryAdapter(SnapshotStore(tmp_path / "store"),
                                                 tmp_path / "cache", Session(Response(records)))
    with pytest.raises(boundary.BoundaryError) as error:
        adapter.collect("Павловск")
    assert error.value.code == code
    if code == "ambiguous_boundary":
        assert len(error.value.details["candidates"]) == 2
        assert all(set(c) == {"osm_type", "osm_id", "name"} for c in error.value.details["candidates"])


def test_duplicate_candidate_is_not_ambiguous(setup):
    adapter, session, _, _ = setup
    session.response = Response([record(), record()])
    assert adapter.collect("Павловск").study_area.area_id


@pytest.mark.parametrize("tamper", ["wrong_query", "snapshot_area", "provenance", "pointer"])
def test_corrupt_cache_never_fetches(setup, tamper):
    adapter, session, cache, store = setup
    adapter.collect("Павловск")
    path = pointer(cache)
    if tamper == "pointer":
        path.write_text("not json", encoding="utf-8")
    elif tamper == "wrong_query":
        metadata = json.loads(path.read_text(encoding="utf-8"))
        other = boundary.NominatimBoundaryAdapter(store, cache, session)
        other_key = boundary.hashlib.sha256(json.dumps([
            boundary.ENDPOINT, {"q": "other", "format": "jsonv2", "addressdetails": "1",
                                "polygon_geojson": "1", "limit": "5", "countrycodes": "ru",
                                "accept-language": "ru"}, boundary.ADAPTER_VERSION],
            sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        (cache / (other_key + ".json")).write_text(json.dumps(metadata), encoding="utf-8")
        path = cache / (other_key + ".json")
    else:
        metadata = json.loads(path.read_text(encoding="utf-8"))
        snapshot_path = store.root / "snapshots" / (metadata["snapshot_id"] + ".json")
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        snapshot["area_id" if tamper == "snapshot_area" else "source_id"] = "tampered"
        snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    calls = len(session.calls)
    with pytest.raises(boundary.BoundaryError) as error:
        adapter.collect("other" if tamper == "wrong_query" else "Павловск")
    assert error.value.code == "invalid_cache"
    assert len(session.calls) == calls


def test_expired_offline_and_fetch_failure_preserve_pointer(setup, clock):
    adapter, session, cache, store = setup
    first = adapter.collect("Павловск")
    path = pointer(cache)
    clock["now"] += timedelta(days=2)
    before = path.read_bytes()
    offline = boundary.NominatimBoundaryAdapter(store, cache, session, offline=True)
    with pytest.raises(boundary.BoundaryError, match="cache_expired"):
        offline.collect("Павловск")
    session.response = requests.exceptions.SSLError("TLS")
    with pytest.raises(boundary.BoundaryError, match="tls_error"):
        adapter.collect("Павловск")
    assert path.read_bytes() == before
    assert store.get_snapshot(first.snapshot.snapshot_id) == first.snapshot
    assert not (cache / "request.lock").exists()


@pytest.mark.parametrize("response,code", [
    (Response(content=b"<html>challenge</html>"), "invalid_response"),
    (Response(status=403), "access_denied"),
    (Response(status=429), "rate_limited"),
    (Response(headers={"Content-Length": str(boundary.MAX_BYTES + 1)}), "response_too_large"),
    (Response(content=b" " * (boundary.MAX_BYTES + 1)), "response_too_large"),
    (requests.exceptions.SSLError("TLS"), "tls_error"),
])
def test_http_failures_and_size_bounds(tmp_path, response, code):
    adapter = boundary.NominatimBoundaryAdapter(SnapshotStore(tmp_path / "store"),
                                                 tmp_path / "cache", Session(response))
    with pytest.raises(boundary.BoundaryError) as error:
        adapter.collect("Павловск")
    assert error.value.code == code
    assert not (tmp_path / "cache" / "request.lock").exists()


def test_lock_and_shared_rate_limit(setup, monkeypatch):
    adapter, session, cache, store = setup
    cache.mkdir()
    lock = cache / "request.lock"
    lock.write_text("foreign", encoding="utf-8")
    with pytest.raises(boundary.BoundaryError, match="request_locked"):
        adapter.collect("Павловск")
    assert lock.read_text(encoding="utf-8") == "foreign" and not session.calls
    lock.unlink()
    adapter.collect("Павловск")
    sleeps = []
    monkeypatch.setattr(boundary.time, "sleep", sleeps.append)
    other = boundary.NominatimBoundaryAdapter(store, cache, Session(Response([record(2)])))
    other.collect("Гатчина")
    assert sleeps and 0 < sleeps[0] <= boundary.INTERVAL
    assert not lock.exists()


@pytest.mark.parametrize("url", ["http://example.org/search", "https://user@example.org/search",
                                     "https://example.org/search?q=x", "https://example.org/search#x",
                                     "https://example.org/lookup", "https://example.org:99999/search"])
def test_operator_endpoint_rejects_unsafe_urls(tmp_path, url):
    with pytest.raises(ValueError, match="endpoint"):
        boundary.NominatimBoundaryAdapter(SnapshotStore(tmp_path), tmp_path / "cache", endpoint=url)


def test_operator_endpoint_needs_terms_and_isolated_cache(setup):
    adapter, session, cache, store = setup
    default = adapter.collect("Павловск")
    endpoint = "https://operator.example.org/search"
    denied = boundary.NominatimBoundaryAdapter(store, cache, session, endpoint=endpoint)
    with pytest.raises(boundary.BoundaryError, match="access_not_permitted"):
        denied.collect("Павловск")
    assert len(session.calls) == 1
    allowed = boundary.NominatimBoundaryAdapter(store, cache, session, endpoint=endpoint,
                                                 terms_confirmed=True)
    allowed.collect("Павловск")
    assert len(session.calls) == 2
    assert session.calls[1][0][0] == endpoint
    assert len([p for p in cache.glob("*.json") if p.name != "last_request.json"]) == 2
    assert default.snapshot.url != allowed.collect("Павловск").snapshot.url


def test_same_osm_id_with_conflicting_geometry_is_not_silently_merged(setup):
    adapter, session, _, _ = setup
    other = {"type": "Polygon", "coordinates": [[[30, 59], [32, 59], [32, 60], [30, 59]]]}
    session.response = Response([record(), record(geojson=other)])
    with pytest.raises(boundary.BoundaryError, match="conflicting_boundary"):
        adapter.collect("Synthetic municipality")


@pytest.mark.parametrize("ttl", [0, -1, True, float("nan"), float("inf"), "1"])
def test_bad_ttl_is_rejected_before_network(tmp_path, ttl):
    with pytest.raises(ValueError):
        boundary.NominatimBoundaryAdapter(SnapshotStore(tmp_path), tmp_path / "cache", ttl_seconds=ttl)


def test_tampered_future_cache_and_rate_timestamps_fail_without_waiting(setup, clock, monkeypatch):
    adapter, session, cache, _ = setup
    adapter.collect("Synthetic municipality")
    path = pointer(cache)
    metadata = json.loads(path.read_text(encoding="utf-8"))
    metadata["checked_at"] = (clock["now"] + timedelta(days=1)).isoformat()
    metadata["expires_at"] = (clock["now"] + timedelta(days=2)).isoformat()
    path.write_text(json.dumps(metadata), encoding="utf-8")
    calls = len(session.calls)
    with pytest.raises(boundary.BoundaryError, match="invalid_cache"):
        adapter.collect("Synthetic municipality")
    assert len(session.calls) == calls
    (cache / "last_request.json").write_text(json.dumps({"at": metadata["checked_at"]}), encoding="utf-8")
    monkeypatch.setattr(boundary.time, "sleep", lambda _: pytest.fail("Future clock must not cause an unbounded wait"))
    with pytest.raises(boundary.BoundaryError, match="invalid_rate_state"):
        adapter.collect("Another synthetic municipality")
    assert len(session.calls) == calls


def test_renewing_cache_does_not_refresh_source_first_retrieval_time(setup, clock):
    adapter, _, _, _ = setup
    first = adapter.collect("Synthetic municipality")
    clock["now"] += timedelta(days=2)
    second = adapter.collect("Synthetic municipality")
    assert not second.from_cache
    assert second.snapshot.snapshot_id == first.snapshot.snapshot_id
    assert second.snapshot.retrieved_at == first.snapshot.retrieved_at
    assert second.observed_at > first.observed_at
