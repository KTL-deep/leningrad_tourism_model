"""Overpass POI adapter: synthetic fixtures, no external requests or real waiting."""

from datetime import datetime, timedelta, timezone
import json

import pytest
import requests

from src.etl.adapters import overpass_poi as poi
from src.data_models.study_area import StudyArea
from src.storage import SnapshotStore


POLYGON = {"type": "Polygon", "coordinates": [[[30, 59], [31, 59], [31, 60], [30, 60], [30, 59]]]}


def _study_area(name="Тестовый район"):
    return StudyArea(name=name, request_geometry=POLYGON)


def _overpass_response(elements=None):
    """Build a synthetic valid Overpass JSON response."""
    if elements is None:
        elements = [
            {"type": "node", "id": 1, "lat": 59.5, "lon": 30.5,
             "tags": {"amenity": "cafe", "name": "Тест-кафе"}},
            {"type": "node", "id": 2, "lat": 59.6, "lon": 30.6,
             "tags": {"tourism": "museum", "name": "Тест-музей"}},
            {"type": "way", "id": 100, "center": {"lat": 59.7, "lon": 30.7},
             "tags": {"historic": "castle", "name": "Тест-замок"}},
            {"type": "relation", "id": 200, "center": {"lat": 59.8, "lon": 30.8},
             "tags": {"leisure": "park", "name": "Тест-парк"}},
            {"type": "node", "id": 3, "lat": 59.55, "lon": 30.55,
             "tags": {"shop": "supermarket", "name": "Тест-магазин"}},
        ]
    return json.dumps({"elements": elements}).encode("utf-8")


class Response:
    def __init__(self, elements=None, *, content=None, status=200, headers=None):
        self.body = content if content is not None else _overpass_response(elements)
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

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.fixture
def clock(monkeypatch):
    state = {"now": datetime(2026, 10, 19, tzinfo=timezone.utc)}
    monkeypatch.setattr(poi, "_now", lambda: state["now"])
    monkeypatch.setattr(poi.time, "sleep",
                        lambda seconds: state.update(now=state["now"] + timedelta(seconds=seconds)))
    return state


@pytest.fixture
def area():
    return _study_area()


@pytest.fixture
def setup(tmp_path, clock, area):
    session = Session(Response())
    cache = tmp_path / "cache"
    store = SnapshotStore(tmp_path / "store")
    adapter = poi.OverpassPOIAdapter(store, cache, session)
    return adapter, session, cache, store, area


def pointer(cache):
    return next(p for p in cache.glob("*.json") if p.name != "last_request.json")


# ─── Query construction tests ───


class TestQueryConstruction:
    def test_query_contains_json_output_and_timeout(self, area):
        query = poi._build_query(area)
        assert "[out:json]" in query
        assert "[timeout:120]" in query
        assert "out center;" in query

    def test_query_contains_all_poi_tag_keys(self, area):
        query = poi._build_query(area)
        for tag_key in poi.POI_TAGS:
            assert f'"{tag_key}"' in query

    def test_query_contains_specific_amenity_values(self, area):
        query = poi._build_query(area)
        for value in poi.POI_TAGS["amenity"]:
            assert value in query

    def test_query_contains_poly_filter_with_coordinates(self, area):
        query = poi._build_query(area)
        assert 'poly:"' in query
        # GeoJSON coordinates are lon,lat; Overpass poly is "lat lon"
        assert "59.0 30.0" in query
        assert "59.0 31.0" in query

    def test_query_includes_node_way_relation(self, area):
        query = poi._build_query(area)
        assert "node[" in query
        assert "way[" in query
        assert "relation[" in query

    def test_historic_uses_any_value_filter(self, area):
        """historic should match all values, not a regex."""
        query = poi._build_query(area)
        # historic filter should be ["historic"] without regex
        assert 'node["historic"]' in query

    def test_draft_characterization_largest_polygon_only(self):
        """Characterize a known draft limitation, NOT the accepted coverage policy."""
        multi = {"type": "MultiPolygon", "coordinates": [
            [[[30, 59], [31, 59], [31, 60], [30, 60], [30, 59]]],
            [[[32, 59], [32.1, 59], [32.1, 59.1], [32, 59.1], [32, 59]]],
        ]}
        area = StudyArea(name="Мульти", request_geometry=multi)
        poly = poi._polygon_filter(area)
        # This draft currently keeps only the larger polygon; the production
        # inventory adapter instead covers every part in an explicit bbox.
        assert "59.0 30.0" in poly

    @pytest.mark.xfail(strict=True, raises=AssertionError,
                       reason="Unconnected POI draft loses smaller MultiPolygon components; not used by new CLI")
    def test_draft_multipolygon_must_not_silently_lose_components(self):
        multi = {"type": "MultiPolygon", "coordinates": [
            [[[30, 59], [31, 59], [31, 60], [30, 60], [30, 59]]],
            [[[32, 59], [32.1, 59], [32.1, 59.1], [32, 59.1], [32, 59]]],
        ]}
        poly = poi._polygon_filter(StudyArea(name="Synthetic multipart", request_geometry=multi))
        assert "59.0 32.0" in poly


# ─── Response parsing tests ───


class TestResponseParsing:
    def test_parse_valid_response(self):
        data = _overpass_response()
        pois, counts = poi._parse_elements(data)
        assert len(pois) == 5
        assert counts["amenity=cafe"] == 1
        assert counts["tourism=museum"] == 1
        assert counts["historic=castle"] == 1
        assert counts["leisure=park"] == 1
        assert counts["shop=supermarket"] == 1

    def test_parse_node_coordinates(self):
        elements = [{"type": "node", "id": 1, "lat": 59.5, "lon": 30.5,
                      "tags": {"amenity": "cafe"}}]
        pois, _ = poi._parse_elements(json.dumps({"elements": elements}).encode())
        assert pois[0]["lat"] == 59.5
        assert pois[0]["lon"] == 30.5

    def test_parse_way_center_coordinates(self):
        elements = [{"type": "way", "id": 100,
                      "center": {"lat": 59.7, "lon": 30.7},
                      "tags": {"tourism": "hotel"}}]
        pois, _ = poi._parse_elements(json.dumps({"elements": elements}).encode())
        assert pois[0]["lat"] == 59.7
        assert pois[0]["lon"] == 30.7

    def test_parse_way_without_center_is_skipped(self):
        elements = [{"type": "way", "id": 100,
                      "tags": {"tourism": "hotel"}}]
        pois, _ = poi._parse_elements(json.dumps({"elements": elements}).encode())
        assert len(pois) == 0

    def test_parse_skips_elements_without_tags(self):
        elements = [{"type": "node", "id": 1, "lat": 59.5, "lon": 30.5}]
        pois, _ = poi._parse_elements(json.dumps({"elements": elements}).encode())
        assert len(pois) == 0

    def test_parse_empty_elements(self):
        pois, counts = poi._parse_elements(json.dumps({"elements": []}).encode())
        assert len(pois) == 0
        assert counts == {}

    def test_parse_rejects_non_dict_root(self):
        with pytest.raises(poi.POIError) as err:
            poi._parse_elements(json.dumps([]).encode())
        assert err.value.code == "invalid_response"

    def test_parse_rejects_missing_elements(self):
        with pytest.raises(poi.POIError) as err:
            poi._parse_elements(json.dumps({"version": 0.6}).encode())
        assert err.value.code == "invalid_response"

    def test_parse_rejects_non_list_elements(self):
        with pytest.raises(poi.POIError) as err:
            poi._parse_elements(json.dumps({"elements": "bad"}).encode())
        assert err.value.code == "invalid_response"

    def test_parse_rejects_non_dict_element(self):
        with pytest.raises(poi.POIError) as err:
            poi._parse_elements(json.dumps({"elements": ["bad"]}).encode())
        assert err.value.code == "invalid_response"

    def test_parse_rejects_invalid_utf8(self):
        with pytest.raises(poi.POIError) as err:
            poi._parse_elements(b"\xff\xfe")
        assert err.value.code == "invalid_response"

    def test_parse_rejects_invalid_json(self):
        with pytest.raises(poi.POIError) as err:
            poi._parse_elements(b"not json at all")
        assert err.value.code == "invalid_response"

    def test_parse_skips_boolean_coordinates(self):
        """bool is a subclass of int; we must reject it."""
        elements = [{"type": "node", "id": 1, "lat": True, "lon": False,
                      "tags": {"amenity": "cafe"}}]
        pois, _ = poi._parse_elements(json.dumps({"elements": elements}).encode())
        assert len(pois) == 0


# ─── Full collect workflow tests ───


class TestCollect:
    def test_fresh_fetch_returns_poi_collection(self, setup, area):
        adapter, session, cache, store, area = setup
        result = adapter.collect(area)
        assert isinstance(result, poi.POICollection)
        assert result.study_area is area
        assert result.poi_count == 5
        assert result.from_cache is False
        assert result.observed_at.tzinfo is not None
        assert result.attribution == poi.ATTRIBUTION
        assert result.snapshot.source_id == poi.SOURCE_ID
        assert result.snapshot.parser_version == poi.ADAPTER_VERSION
        assert result.snapshot.area_id == area.area_id

    def test_collect_sends_post_with_correct_params(self, setup, area):
        adapter, session, cache, store, area = setup
        adapter.collect(area)
        assert len(session.calls) == 1
        _args, kwargs = session.calls[0]
        assert kwargs["headers"]["User-Agent"] == poi.USER_AGENT
        assert kwargs["verify"] is True
        assert "data" in kwargs
        assert "[out:json]" in kwargs["data"]["data"]

    def test_collect_rejects_non_study_area(self, setup):
        adapter, *_ = setup
        with pytest.raises(poi.POIError) as err:
            adapter.collect("not a study area")
        assert err.value.code == "invalid_study_area"

    def test_category_counts_are_correct(self, setup, area):
        adapter, *_ = setup
        result = adapter.collect(area)
        assert result.category_counts["amenity=cafe"] == 1
        assert result.category_counts["tourism=museum"] == 1
        assert result.category_counts["historic=castle"] == 1


# ─── Cache hit/miss/expiry tests ───


class TestCache:
    def test_cache_hit(self, setup, area, clock):
        adapter, session, cache, store, area = setup
        first = adapter.collect(area)
        assert not first.from_cache
        # Second call should hit cache
        second = adapter.collect(area)
        assert second.from_cache
        assert second.snapshot.snapshot_id == first.snapshot.snapshot_id
        assert len(session.calls) == 1

    def test_cache_expiry_triggers_refetch(self, setup, area, clock):
        adapter, session, cache, store, area = setup
        first = adapter.collect(area)
        clock["now"] += timedelta(days=2)
        second = adapter.collect(area)
        assert not second.from_cache
        assert len(session.calls) == 2

    def test_cache_miss_offline_raises(self, tmp_path, area, clock):
        store = SnapshotStore(tmp_path / "store")
        adapter = poi.OverpassPOIAdapter(store, tmp_path / "cache",
                                          Session(AssertionError("no network")), offline=True)
        with pytest.raises(poi.POIError) as err:
            adapter.collect(area)
        assert err.value.code == "cache_miss"

    def test_cache_expired_offline_raises(self, setup, area, clock):
        adapter, session, cache, store, area = setup
        adapter.collect(area)
        clock["now"] += timedelta(days=2)
        offline = poi.OverpassPOIAdapter(store, cache,
                                          Session(AssertionError("no network")), offline=True)
        with pytest.raises(poi.POIError) as err:
            offline.collect(area)
        assert err.value.code == "cache_expired"

    def test_offline_cache_hit_returns_data(self, setup, area, clock):
        adapter, session, cache, store, area = setup
        first = adapter.collect(area)
        offline = poi.OverpassPOIAdapter(store, cache,
                                          Session(AssertionError("no network")), offline=True)
        second = offline.collect(area)
        assert second.from_cache
        assert second.snapshot.snapshot_id == first.snapshot.snapshot_id
        assert len(session.calls) == 1

    def test_corrupt_cache_pointer_raises(self, setup, area):
        adapter, session, cache, store, area = setup
        adapter.collect(area)
        path = pointer(cache)
        path.write_text("not json", encoding="utf-8")
        with pytest.raises(poi.POIError) as err:
            adapter.collect(area)
        assert err.value.code == "invalid_cache"

    def test_tampered_snapshot_provenance_raises(self, setup, area):
        adapter, session, cache, store, area = setup
        adapter.collect(area)
        path = pointer(cache)
        metadata = json.loads(path.read_text(encoding="utf-8"))
        snapshot_path = store.root / "snapshots" / (metadata["snapshot_id"] + ".json")
        snapshot_data = json.loads(snapshot_path.read_text(encoding="utf-8"))
        snapshot_data["source_id"] = "tampered"
        snapshot_path.write_text(json.dumps(snapshot_data), encoding="utf-8")
        calls = len(session.calls)
        with pytest.raises(poi.POIError) as err:
            adapter.collect(area)
        assert err.value.code == "invalid_cache"
        assert len(session.calls) == calls

    def test_future_cache_timestamp_raises(self, setup, area, clock):
        adapter, session, cache, store, area = setup
        adapter.collect(area)
        path = pointer(cache)
        metadata = json.loads(path.read_text(encoding="utf-8"))
        metadata["checked_at"] = (clock["now"] + timedelta(days=1)).isoformat()
        metadata["expires_at"] = (clock["now"] + timedelta(days=2)).isoformat()
        path.write_text(json.dumps(metadata), encoding="utf-8")
        calls = len(session.calls)
        with pytest.raises(poi.POIError) as err:
            adapter.collect(area)
        assert err.value.code == "invalid_cache"
        assert len(session.calls) == calls

    def test_renewing_cache_preserves_original_retrieval_time(self, setup, area, clock):
        adapter, _, _, _, area = setup
        first = adapter.collect(area)
        clock["now"] += timedelta(days=2)
        second = adapter.collect(area)
        assert not second.from_cache
        assert second.snapshot.snapshot_id == first.snapshot.snapshot_id
        assert second.snapshot.retrieved_at == first.snapshot.retrieved_at
        assert second.observed_at > first.observed_at


# ─── HTTP error handling tests ───


class TestHTTPErrors:
    @pytest.mark.parametrize("response,code", [
        (Response(content=b"<html>challenge</html>"), "invalid_response"),
        (Response(status=403), "access_denied"),
        (Response(status=429), "rate_limited"),
        (Response(status=500), "http_error"),
        (Response(headers={"Content-Length": str(poi.MAX_BYTES + 1)}), "response_too_large"),
        (Response(content=b" " * (poi.MAX_BYTES + 1)), "response_too_large"),
    ])
    def test_http_failures_and_size_bounds(self, tmp_path, response, code, monkeypatch):
        monkeypatch.setattr(poi, "_now", lambda: datetime(2026, 10, 19, tzinfo=timezone.utc))
        monkeypatch.setattr(poi.time, "sleep", lambda _: None)
        adapter = poi.OverpassPOIAdapter(SnapshotStore(tmp_path / "store"),
                                          tmp_path / "cache", Session(response))
        with pytest.raises(poi.POIError) as err:
            adapter.collect(_study_area())
        assert err.value.code == code
        assert not (tmp_path / "cache" / "request.lock").exists()

    def test_ssl_error_raises_tls_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(poi, "_now", lambda: datetime(2026, 10, 19, tzinfo=timezone.utc))
        monkeypatch.setattr(poi.time, "sleep", lambda _: None)
        adapter = poi.OverpassPOIAdapter(SnapshotStore(tmp_path / "store"),
                                          tmp_path / "cache",
                                          Session(requests.exceptions.SSLError("TLS")))
        with pytest.raises(poi.POIError) as err:
            adapter.collect(_study_area())
        assert err.value.code == "tls_error"

    def test_timeout_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(poi, "_now", lambda: datetime(2026, 10, 19, tzinfo=timezone.utc))
        monkeypatch.setattr(poi.time, "sleep", lambda _: None)
        adapter = poi.OverpassPOIAdapter(SnapshotStore(tmp_path / "store"),
                                          tmp_path / "cache",
                                          Session(requests.exceptions.Timeout("timeout")))
        with pytest.raises(poi.POIError) as err:
            adapter.collect(_study_area())
        assert err.value.code == "timeout"

    def test_connection_error_raises_fetch_failed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(poi, "_now", lambda: datetime(2026, 10, 19, tzinfo=timezone.utc))
        monkeypatch.setattr(poi.time, "sleep", lambda _: None)
        adapter = poi.OverpassPOIAdapter(SnapshotStore(tmp_path / "store"),
                                          tmp_path / "cache",
                                          Session(requests.exceptions.ConnectionError("refused")))
        with pytest.raises(poi.POIError) as err:
            adapter.collect(_study_area())
        assert err.value.code == "fetch_failed"

    def test_malformed_json_response(self, tmp_path, monkeypatch):
        monkeypatch.setattr(poi, "_now", lambda: datetime(2026, 10, 19, tzinfo=timezone.utc))
        monkeypatch.setattr(poi.time, "sleep", lambda _: None)
        adapter = poi.OverpassPOIAdapter(SnapshotStore(tmp_path / "store"),
                                          tmp_path / "cache",
                                          Session(Response(content=b"{invalid json")))
        with pytest.raises(poi.POIError) as err:
            adapter.collect(_study_area())
        assert err.value.code == "invalid_response"


# ─── Rate limiting and lock tests ───


class TestRateLimiting:
    def test_lock_prevents_concurrent_requests(self, setup, area):
        adapter, session, cache, store, area = setup
        cache.mkdir(parents=True, exist_ok=True)
        lock = cache / "request.lock"
        lock.write_text("foreign", encoding="utf-8")
        with pytest.raises(poi.POIError) as err:
            adapter.collect(area)
        assert err.value.code == "request_locked"
        assert lock.read_text(encoding="utf-8") == "foreign"
        assert not session.calls

    def test_lock_is_released_after_success(self, setup, area):
        adapter, session, cache, store, area = setup
        adapter.collect(area)
        assert not (cache / "request.lock").exists()

    def test_lock_is_released_after_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(poi, "_now", lambda: datetime(2026, 10, 19, tzinfo=timezone.utc))
        monkeypatch.setattr(poi.time, "sleep", lambda _: None)
        adapter = poi.OverpassPOIAdapter(SnapshotStore(tmp_path / "store"),
                                          tmp_path / "cache", Session(Response(status=500)))
        with pytest.raises(poi.POIError):
            adapter.collect(_study_area())
        assert not (tmp_path / "cache" / "request.lock").exists()

    def test_rate_limit_sleep_is_applied(self, setup, area, monkeypatch):
        adapter, session, cache, store, area = setup
        adapter.collect(area)
        sleeps = []
        monkeypatch.setattr(poi.time, "sleep", sleeps.append)
        session2 = Session(Response())
        other = poi.OverpassPOIAdapter(store, cache, session2)
        # Use different study area to avoid cache hit
        other_area = StudyArea(name="Другой район", request_geometry={
            "type": "Polygon", "coordinates": [[[31, 59], [32, 59], [32, 60], [31, 60], [31, 59]]]
        })
        other.collect(other_area)
        assert sleeps and 0 < sleeps[0] <= poi.INTERVAL

    def test_future_rate_timestamp_raises(self, setup, area, clock, monkeypatch):
        adapter, session, cache, store, area = setup
        adapter.collect(area)
        (cache / "last_request.json").write_text(
            json.dumps({"at": (clock["now"] + timedelta(days=1)).isoformat()}), encoding="utf-8")
        monkeypatch.setattr(poi.time, "sleep",
                            lambda _: pytest.fail("Future clock must not cause unbounded wait"))
        # Use different study area to avoid cache hit
        other_area = StudyArea(name="Другой район", request_geometry={
            "type": "Polygon", "coordinates": [[[31, 59], [32, 59], [32, 60], [31, 60], [31, 59]]]
        })
        with pytest.raises(poi.POIError) as err:
            adapter.collect(other_area)
        assert err.value.code == "invalid_rate_state"


# ─── Snapshot storage integration tests ───


class TestSnapshotStorage:
    def test_snapshot_is_persisted_and_retrievable(self, setup, area):
        adapter, session, cache, store, area = setup
        result = adapter.collect(area)
        retrieved = store.get_snapshot(result.snapshot.snapshot_id)
        assert retrieved.source_id == poi.SOURCE_ID
        assert retrieved.area_id == area.area_id
        assert retrieved.parser_version == poi.ADAPTER_VERSION
        assert retrieved.media_type == "application/json"
        assert retrieved.completeness == "complete"

    def test_snapshot_content_is_verifiable(self, setup, area):
        adapter, session, cache, store, area = setup
        result = adapter.collect(area)
        content = store.read_content(result.snapshot.snapshot_id)
        parsed = json.loads(content)
        assert "elements" in parsed

    def test_duplicate_fetch_reuses_snapshot(self, setup, area, clock):
        adapter, _, _, store, area = setup
        first = adapter.collect(area)
        clock["now"] += timedelta(days=2)
        second = adapter.collect(area)
        assert second.snapshot.snapshot_id == first.snapshot.snapshot_id


# ─── Constructor validation tests ───


class TestConstructorValidation:
    @pytest.mark.parametrize("ttl", [0, -1, True, float("nan"), float("inf"), "1"])
    def test_bad_ttl_is_rejected(self, tmp_path, ttl):
        with pytest.raises(ValueError):
            poi.OverpassPOIAdapter(SnapshotStore(tmp_path), tmp_path / "cache", ttl_seconds=ttl)

    @pytest.mark.parametrize("url", [
        "http://example.org/api/interpreter",
        "https://user@example.org/api/interpreter",
        "https://example.org/api/interpreter?q=x",
        "https://example.org/api/interpreter#x",
        "https://example.org/search",
    ])
    def test_rejects_unsafe_endpoint_urls(self, tmp_path, url):
        with pytest.raises(ValueError, match="endpoint"):
            poi.OverpassPOIAdapter(SnapshotStore(tmp_path), tmp_path / "cache", endpoint=url)

    def test_offline_must_be_bool(self, tmp_path):
        with pytest.raises(ValueError):
            poi.OverpassPOIAdapter(SnapshotStore(tmp_path), tmp_path / "cache", offline="yes")


# ─── POIError tests ───


class TestPOIError:
    def test_error_has_code_and_details(self):
        err = poi.POIError("test_code", details={"key": "val"})
        assert err.code == "test_code"
        assert err.details == {"key": "val"}
        assert str(err) == "test_code"

    def test_error_inherits_from_exception(self):
        assert issubclass(poi.POIError, Exception)


# ─── Constants tests ───


class TestConstants:
    def test_source_id(self):
        assert poi.SOURCE_ID == "osm_overpass_poi"

    def test_adapter_version(self):
        assert poi.ADAPTER_VERSION == "overpass-poi-v1"

    def test_endpoint(self):
        assert poi.ENDPOINT == "https://overpass-api.de/api/interpreter"

    def test_max_bytes(self):
        assert poi.MAX_BYTES == 50 * 1024 * 1024

    def test_attribution(self):
        assert poi.ATTRIBUTION == "© OpenStreetMap contributors (ODbL)"

    def test_poi_tags_contain_required_categories(self):
        assert "amenity" in poi.POI_TAGS
        assert "tourism" in poi.POI_TAGS
        assert "historic" in poi.POI_TAGS
        assert "leisure" in poi.POI_TAGS
        assert "shop" in poi.POI_TAGS
        # historic accepts all values
        assert poi.POI_TAGS["historic"] is None
