"""Comprehensive offline tests for the Overpass network adapter."""

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.data_models.study_area import StudyArea
from src.storage import SnapshotStore
from src.etl.adapters.overpass_network import (
    ADAPTER_VERSION,
    ATTRIBUTION,
    ENDPOINT,
    HIGHWAY_VALUES,
    MAX_BYTES,
    NetworkCollection,
    NetworkError,
    OverpassNetworkAdapter,
    RAILWAY_VALUES,
    ROUTE_VALUES,
    SOURCE_ID,
    _parse_response,
    _polygon_coords,
    build_query,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SQUARE = {
    "type": "Polygon",
    "coordinates": [[[30.0, 59.0], [31.0, 59.0], [31.0, 60.0], [30.0, 60.0], [30.0, 59.0]]],
}

_LARGER_SQUARE = {
    "type": "Polygon",
    "coordinates": [[[29.0, 58.0], [32.0, 58.0], [32.0, 61.0], [29.0, 61.0], [29.0, 58.0]]],
}


def _make_study_area(*, analysis_geometry=None):
    """Create a synthetic StudyArea for testing."""
    return StudyArea(
        name="Test Area",
        request_geometry=_SQUARE,
        analysis_geometry=analysis_geometry,
    )


def _valid_overpass_response(*, ways=2, stations=1, routes=1):
    """Build a synthetic valid Overpass JSON response."""
    elements = []

    for i in range(ways):
        elements.append({
            "type": "way",
            "id": 100 + i,
            "tags": {"highway": "primary"},
            "geometry": [
                {"lat": 59.0 + i * 0.01, "lon": 30.0},
                {"lat": 59.0 + i * 0.01, "lon": 30.1},
                {"lat": 59.0 + i * 0.01, "lon": 30.2},
            ],
        })

    for i in range(stations):
        elements.append({
            "type": "node",
            "id": 200 + i,
            "lat": 59.5,
            "lon": 30.5,
            "tags": {"railway": "station", "name": f"Station {i}"},
        })

    for i in range(routes):
        elements.append({
            "type": "relation",
            "id": 300 + i,
            "tags": {"route": "bus", "ref": f"Route {i}"},
            "members": [],
        })

    return json.dumps({"version": 0.6, "elements": elements}).encode("utf-8")


@pytest.fixture
def study_area():
    return _make_study_area()


@pytest.fixture
def study_area_with_analysis():
    return _make_study_area(analysis_geometry=_LARGER_SQUARE)


@pytest.fixture
def store(tmp_path):
    return SnapshotStore(tmp_path / "store")


@pytest.fixture
def cache_dir(tmp_path):
    d = tmp_path / "cache"
    d.mkdir()
    return d


@pytest.fixture
def adapter(store, cache_dir):
    return OverpassNetworkAdapter(store, cache_dir, offline=False)


# ---------------------------------------------------------------------------
# Query construction
# ---------------------------------------------------------------------------


class TestBuildQuery:
    def test_query_contains_highway_values(self, study_area):
        query = build_query(study_area)
        for val in HIGHWAY_VALUES:
            assert val in query

    def test_query_contains_railway_values(self, study_area):
        query = build_query(study_area)
        for val in RAILWAY_VALUES:
            assert val in query

    def test_query_contains_route_values(self, study_area):
        query = build_query(study_area)
        for val in ROUTE_VALUES:
            assert val in query

    def test_query_timeout_and_format(self, study_area):
        query = build_query(study_area)
        assert "[out:json]" in query
        assert "[timeout:180]" in query

    def test_query_out_geom(self, study_area):
        query = build_query(study_area)
        assert "out geom" in query

    def test_query_uses_poly_filter(self, study_area):
        query = build_query(study_area)
        assert 'poly:"' in query

    def test_query_has_correct_coordinates(self, study_area):
        query = build_query(study_area)
        # The polygon has vertices (30,59), (31,59), (31,60), (30,60), (30,59)
        # In Overpass poly format: "lat lon" → "59.0 30.0 59.0 31.0 ..."
        assert "59.0 30.0" in query
        assert "59.0 31.0" in query

    def test_query_way_and_node_and_relation(self, study_area):
        query = build_query(study_area)
        assert "way[" in query
        assert "node[" in query
        assert "relation[" in query


class TestPolygonCoords:
    def test_uses_request_geometry_by_default(self, study_area):
        coords = _polygon_coords(study_area)
        # Request geometry is _SQUARE whose first point is lon=30, lat=59
        assert "59.0 30.0" in coords

    def test_uses_analysis_geometry_when_available(self, study_area_with_analysis):
        coords = _polygon_coords(study_area_with_analysis)
        # Analysis geometry is _LARGER_SQUARE whose first point is lon=29, lat=58
        assert "58.0 29.0" in coords
        # Should NOT use request geometry points exclusively
        assert "58.0 29.0" in coords

    def test_multipolygon_coords(self):
        multi = {
            "type": "MultiPolygon",
            "coordinates": [
                [[[30.0, 59.0], [31.0, 59.0], [31.0, 60.0], [30.0, 60.0], [30.0, 59.0]]],
                [[[32.0, 59.0], [33.0, 59.0], [33.0, 60.0], [32.0, 60.0], [32.0, 59.0]]],
            ],
        }
        # MultiPolygon must cover Polygon for StudyArea validation, so build a
        # request_geometry that fits inside
        small = {
            "type": "Polygon",
            "coordinates": [[[30.1, 59.1], [30.9, 59.1], [30.9, 59.9], [30.1, 59.9], [30.1, 59.1]]],
        }
        area = StudyArea(name="Multi", request_geometry=small, analysis_geometry=multi)
        coords = _polygon_coords(area)
        # Both polygon sets should appear
        assert "59.0 32.0" in coords
        assert "59.0 30.0" in coords


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


class TestParseResponse:
    def test_valid_response(self):
        raw = _valid_overpass_response(ways=3, stations=2, routes=1)
        way_count, station_count, route_count = _parse_response(raw)
        assert way_count == 3
        assert station_count == 2
        assert route_count == 1

    def test_empty_elements(self):
        raw = json.dumps({"version": 0.6, "elements": []}).encode("utf-8")
        way_count, station_count, route_count = _parse_response(raw)
        assert way_count == 0
        assert station_count == 0
        assert route_count == 0

    def test_degenerate_way_skipped(self):
        """Ways with fewer than 2 geometry points are skipped."""
        raw = json.dumps({"version": 0.6, "elements": [
            {"type": "way", "id": 1, "tags": {"highway": "primary"},
             "geometry": [{"lat": 59.0, "lon": 30.0}]},
        ]}).encode("utf-8")
        way_count, _, _ = _parse_response(raw)
        assert way_count == 0

    def test_way_missing_geometry_skipped(self):
        raw = json.dumps({"version": 0.6, "elements": [
            {"type": "way", "id": 1, "tags": {"highway": "primary"}},
        ]}).encode("utf-8")
        way_count, _, _ = _parse_response(raw)
        assert way_count == 0

    def test_station_halt(self):
        raw = json.dumps({"version": 0.6, "elements": [
            {"type": "node", "id": 1, "lat": 59.0, "lon": 30.0,
             "tags": {"railway": "halt"}},
        ]}).encode("utf-8")
        _, station_count, _ = _parse_response(raw)
        assert station_count == 1

    def test_node_without_railway_tag_not_counted(self):
        raw = json.dumps({"version": 0.6, "elements": [
            {"type": "node", "id": 1, "lat": 59.0, "lon": 30.0, "tags": {}},
        ]}).encode("utf-8")
        _, station_count, _ = _parse_response(raw)
        assert station_count == 0

    def test_invalid_json_raises(self):
        with pytest.raises(NetworkError, match="invalid_response"):
            _parse_response(b"not json")

    def test_missing_elements_key_raises(self):
        with pytest.raises(NetworkError, match="invalid_response"):
            _parse_response(json.dumps({"version": 0.6}).encode("utf-8"))

    def test_elements_not_list_raises(self):
        with pytest.raises(NetworkError, match="invalid_response"):
            _parse_response(json.dumps({"elements": "bad"}).encode("utf-8"))

    def test_element_not_dict_raises(self):
        with pytest.raises(NetworkError, match="invalid_response"):
            _parse_response(json.dumps({"elements": ["bad"]}).encode("utf-8"))

    def test_tags_not_dict_raises(self):
        with pytest.raises(NetworkError, match="invalid_response"):
            _parse_response(json.dumps({"elements": [
                {"type": "way", "id": 1, "tags": "bad"},
            ]}).encode("utf-8"))

    def test_route_relation_counted(self):
        raw = json.dumps({"version": 0.6, "elements": [
            {"type": "relation", "id": 1, "tags": {"route": "tram"}, "members": []},
            {"type": "relation", "id": 2, "tags": {"route": "trolleybus"}, "members": []},
        ]}).encode("utf-8")
        _, _, route_count = _parse_response(raw)
        assert route_count == 2

    def test_relation_without_route_tag_not_counted(self):
        raw = json.dumps({"version": 0.6, "elements": [
            {"type": "relation", "id": 1, "tags": {"type": "boundary"}, "members": []},
        ]}).encode("utf-8")
        _, _, route_count = _parse_response(raw)
        assert route_count == 0


# ---------------------------------------------------------------------------
# Adapter initialization
# ---------------------------------------------------------------------------


class TestAdapterInit:
    def test_invalid_ttl(self, store, cache_dir):
        with pytest.raises(ValueError, match="ttl_seconds"):
            OverpassNetworkAdapter(store, cache_dir, ttl_seconds=-1)

    def test_bool_ttl_rejected(self, store, cache_dir):
        with pytest.raises(ValueError, match="ttl_seconds"):
            OverpassNetworkAdapter(store, cache_dir, ttl_seconds=True)

    def test_invalid_offline(self, store, cache_dir):
        with pytest.raises(ValueError, match="offline"):
            OverpassNetworkAdapter(store, cache_dir, offline="yes")

    def test_invalid_endpoint(self, store, cache_dir):
        with pytest.raises(ValueError, match="endpoint"):
            OverpassNetworkAdapter(store, cache_dir, endpoint="http://insecure.example.com/api")

    def test_valid_construction(self, store, cache_dir):
        adapter = OverpassNetworkAdapter(store, cache_dir)
        assert adapter.offline is False
        assert adapter.ttl_seconds == 86400


# ---------------------------------------------------------------------------
# Collect – cache miss → fetch
# ---------------------------------------------------------------------------


class TestCollectFetch:
    def _mock_response(self, content, status_code=200, content_type="application/json"):
        resp = MagicMock()
        resp.status_code = status_code
        resp.headers = {"Content-Type": content_type}
        resp.iter_content = MagicMock(return_value=[content])
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_fresh_fetch(self, store, cache_dir, study_area):
        content = _valid_overpass_response(ways=5, stations=3, routes=2)
        session = MagicMock()
        session.post = MagicMock(return_value=self._mock_response(content))
        adapter = OverpassNetworkAdapter(store, cache_dir, session=session)

        result = adapter.collect(study_area)

        assert isinstance(result, NetworkCollection)
        assert result.from_cache is False
        assert result.way_count == 5
        assert result.station_count == 3
        assert result.route_count == 2
        assert result.attribution == ATTRIBUTION
        assert result.study_area is study_area
        session.post.assert_called_once()

    def test_snapshot_stored_after_fetch(self, store, cache_dir, study_area):
        content = _valid_overpass_response(ways=1, stations=1, routes=0)
        session = MagicMock()
        session.post = MagicMock(return_value=self._mock_response(content))
        adapter = OverpassNetworkAdapter(store, cache_dir, session=session)

        result = adapter.collect(study_area)
        snapshot = result.snapshot
        assert snapshot.source_id == SOURCE_ID
        assert snapshot.parser_version == ADAPTER_VERSION
        assert snapshot.completeness == "complete"
        # Verify content is retrievable
        raw = store.read_content(snapshot.snapshot_id)
        assert raw == content

    def test_cache_pointer_written(self, store, cache_dir, study_area):
        content = _valid_overpass_response()
        session = MagicMock()
        session.post = MagicMock(return_value=self._mock_response(content))
        adapter = OverpassNetworkAdapter(store, cache_dir, session=session)

        adapter.collect(study_area)

        # Should have written exactly one .json cache pointer
        pointers = list(cache_dir.glob("*.json"))
        # Filter out last_request.json
        pointers = [p for p in pointers if p.name != "last_request.json"]
        assert len(pointers) == 1
        data = json.loads(pointers[0].read_text(encoding="utf-8"))
        assert data["schema_version"] == 1
        assert "snapshot_id" in data

    def test_invalid_study_area(self, adapter):
        with pytest.raises(NetworkError, match="invalid_study_area"):
            adapter.collect("not a study area")


# ---------------------------------------------------------------------------
# Collect – cache hit / miss / expiry
# ---------------------------------------------------------------------------


class TestCollectCache:
    def _populate_cache(self, store, cache_dir, study_area, *,
                        ttl_seconds=86400, delta_seconds=0):
        """Populate the cache manually with valid data and return the adapter."""
        content = _valid_overpass_response(ways=4, stations=2, routes=1)
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"Content-Type": "application/json"}
        resp.iter_content = MagicMock(return_value=[content])
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        session.post = MagicMock(return_value=resp)

        adapter = OverpassNetworkAdapter(store, cache_dir, session=session, ttl_seconds=ttl_seconds)
        result = adapter.collect(study_area)
        session.post.reset_mock()

        if delta_seconds != 0:
            # Modify cache pointer times to simulate time passage
            pointers = [p for p in cache_dir.glob("*.json") if p.name != "last_request.json"]
            for p in pointers:
                data = json.loads(p.read_text(encoding="utf-8"))
                checked = datetime.fromisoformat(data["checked_at"])
                shifted = checked - timedelta(seconds=delta_seconds)
                data["checked_at"] = shifted.isoformat()
                data["expires_at"] = (shifted + timedelta(seconds=ttl_seconds)).isoformat()
                p.write_text(json.dumps(data), encoding="utf-8")

        return adapter, session, result

    def test_cache_hit(self, store, cache_dir, study_area):
        adapter, session, _ = self._populate_cache(store, cache_dir, study_area)

        result = adapter.collect(study_area)
        assert result.from_cache is True
        assert result.way_count == 4
        assert result.station_count == 2
        assert result.route_count == 1
        session.post.assert_not_called()

    def test_cache_expired_refetches(self, store, cache_dir, study_area):
        adapter, session, _ = self._populate_cache(
            store, cache_dir, study_area,
            ttl_seconds=60, delta_seconds=120,
        )
        # Make session return fresh data
        fresh = _valid_overpass_response(ways=10, stations=5, routes=3)
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"Content-Type": "application/json"}
        resp.iter_content = MagicMock(return_value=[fresh])
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        session.post = MagicMock(return_value=resp)

        result = adapter.collect(study_area)
        assert result.from_cache is False
        assert result.way_count == 10
        session.post.assert_called_once()

    def test_cache_miss_offline_raises(self, store, cache_dir, study_area):
        adapter = OverpassNetworkAdapter(store, cache_dir, offline=True)
        with pytest.raises(NetworkError) as exc_info:
            adapter.collect(study_area)
        assert exc_info.value.code == "cache_miss"

    def test_cache_expired_offline_raises(self, store, cache_dir, study_area):
        adapter, _, _ = self._populate_cache(
            store, cache_dir, study_area,
            ttl_seconds=60, delta_seconds=120,
        )
        offline_adapter = OverpassNetworkAdapter(store, cache_dir, offline=True, ttl_seconds=60)
        with pytest.raises(NetworkError) as exc_info:
            offline_adapter.collect(study_area)
        assert exc_info.value.code == "cache_expired"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def _mock_response(self, status_code, content=b"", content_type="application/json"):
        resp = MagicMock()
        resp.status_code = status_code
        resp.headers = {"Content-Type": content_type}
        resp.iter_content = MagicMock(return_value=[content])
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_http_403(self, store, cache_dir, study_area):
        session = MagicMock()
        session.post = MagicMock(return_value=self._mock_response(403))
        adapter = OverpassNetworkAdapter(store, cache_dir, session=session)
        with pytest.raises(NetworkError) as exc_info:
            adapter.collect(study_area)
        assert exc_info.value.code == "access_denied"

    def test_http_429(self, store, cache_dir, study_area):
        session = MagicMock()
        session.post = MagicMock(return_value=self._mock_response(429))
        adapter = OverpassNetworkAdapter(store, cache_dir, session=session)
        with pytest.raises(NetworkError) as exc_info:
            adapter.collect(study_area)
        assert exc_info.value.code == "rate_limited"

    def test_http_500(self, store, cache_dir, study_area):
        session = MagicMock()
        session.post = MagicMock(return_value=self._mock_response(500))
        adapter = OverpassNetworkAdapter(store, cache_dir, session=session)
        with pytest.raises(NetworkError) as exc_info:
            adapter.collect(study_area)
        assert exc_info.value.code == "http_error"

    def test_timeout(self, store, cache_dir, study_area):
        import requests as req
        session = MagicMock()
        session.post = MagicMock(side_effect=req.exceptions.Timeout("timeout"))
        adapter = OverpassNetworkAdapter(store, cache_dir, session=session)
        with pytest.raises(NetworkError) as exc_info:
            adapter.collect(study_area)
        assert exc_info.value.code == "timeout"

    def test_ssl_error(self, store, cache_dir, study_area):
        import requests as req
        session = MagicMock()
        session.post = MagicMock(side_effect=req.exceptions.SSLError("ssl"))
        adapter = OverpassNetworkAdapter(store, cache_dir, session=session)
        with pytest.raises(NetworkError) as exc_info:
            adapter.collect(study_area)
        assert exc_info.value.code == "tls_error"

    def test_connection_error(self, store, cache_dir, study_area):
        import requests as req
        session = MagicMock()
        session.post = MagicMock(side_effect=req.exceptions.ConnectionError("conn"))
        adapter = OverpassNetworkAdapter(store, cache_dir, session=session)
        with pytest.raises(NetworkError) as exc_info:
            adapter.collect(study_area)
        assert exc_info.value.code == "fetch_failed"

    def test_response_too_large(self, store, cache_dir, study_area):
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"Content-Type": "application/json", "Content-Length": str(MAX_BYTES + 1)}
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        session.post = MagicMock(return_value=resp)
        adapter = OverpassNetworkAdapter(store, cache_dir, session=session)
        with pytest.raises(NetworkError) as exc_info:
            adapter.collect(study_area)
        assert exc_info.value.code == "response_too_large"

    def test_invalid_content_type(self, store, cache_dir, study_area):
        session = MagicMock()
        session.post = MagicMock(return_value=self._mock_response(
            200, content=b"{}", content_type="text/html"))
        adapter = OverpassNetworkAdapter(store, cache_dir, session=session)
        with pytest.raises(NetworkError) as exc_info:
            adapter.collect(study_area)
        assert exc_info.value.code == "invalid_response"

    def test_lock_contention(self, store, cache_dir, study_area):
        # Create the lock file to simulate contention
        cache_dir.mkdir(parents=True, exist_ok=True)
        lock = cache_dir / "request.lock"
        lock.write_text("other-token", encoding="utf-8")
        session = MagicMock()
        adapter = OverpassNetworkAdapter(store, cache_dir, session=session)
        with pytest.raises(NetworkError) as exc_info:
            adapter.collect(study_area)
        assert exc_info.value.code == "request_locked"
        # Clean up
        lock.unlink()


# ---------------------------------------------------------------------------
# Offline mode
# ---------------------------------------------------------------------------


class TestOfflineMode:
    def test_offline_no_cache_raises_cache_miss(self, store, cache_dir, study_area):
        adapter = OverpassNetworkAdapter(store, cache_dir, offline=True)
        with pytest.raises(NetworkError) as exc_info:
            adapter.collect(study_area)
        assert exc_info.value.code == "cache_miss"

    def test_offline_with_valid_cache_returns(self, store, cache_dir, study_area):
        # First populate cache online
        content = _valid_overpass_response(ways=2, stations=1, routes=1)
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"Content-Type": "application/json"}
        resp.iter_content = MagicMock(return_value=[content])
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        session.post = MagicMock(return_value=resp)
        online = OverpassNetworkAdapter(store, cache_dir, session=session, ttl_seconds=86400)
        online.collect(study_area)

        # Now create offline adapter and it should find the cache
        offline = OverpassNetworkAdapter(store, cache_dir, offline=True, ttl_seconds=86400)
        result = offline.collect(study_area)
        assert result.from_cache is True
        assert result.way_count == 2


# ---------------------------------------------------------------------------
# Snapshot storage integration
# ---------------------------------------------------------------------------


class TestSnapshotIntegration:
    def test_snapshot_content_hash_matches(self, store, cache_dir, study_area):
        import hashlib

        content = _valid_overpass_response(ways=1, stations=0, routes=0)
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"Content-Type": "application/json"}
        resp.iter_content = MagicMock(return_value=[content])
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        session.post = MagicMock(return_value=resp)
        adapter = OverpassNetworkAdapter(store, cache_dir, session=session)

        result = adapter.collect(study_area)
        snapshot = result.snapshot

        # Verify we can read the content back and it matches
        raw = store.read_content(snapshot.snapshot_id)
        assert raw == content
        assert snapshot.content_hash == hashlib.sha256(content).hexdigest()
        assert snapshot.byte_length == len(content)

    def test_snapshot_area_id_matches_study_area(self, store, cache_dir, study_area):
        content = _valid_overpass_response()
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"Content-Type": "application/json"}
        resp.iter_content = MagicMock(return_value=[content])
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        session.post = MagicMock(return_value=resp)
        adapter = OverpassNetworkAdapter(store, cache_dir, session=session)

        result = adapter.collect(study_area)
        assert result.snapshot.area_id == study_area.area_id


# ---------------------------------------------------------------------------
# NetworkCollection dataclass
# ---------------------------------------------------------------------------


class TestNetworkCollection:
    def test_frozen(self, study_area):
        nc = NetworkCollection(
            study_area=study_area, snapshot=None, way_count=1,
            station_count=0, route_count=0, from_cache=False,
            observed_at=datetime.now(timezone.utc),
        )
        with pytest.raises(AttributeError):
            nc.way_count = 99

    def test_default_attribution(self, study_area):
        nc = NetworkCollection(
            study_area=study_area, snapshot=None, way_count=0,
            station_count=0, route_count=0, from_cache=False,
            observed_at=datetime.now(timezone.utc),
        )
        assert nc.attribution == ATTRIBUTION


# ---------------------------------------------------------------------------
# NetworkError
# ---------------------------------------------------------------------------


class TestNetworkError:
    def test_error_code(self):
        err = NetworkError("cache_miss")
        assert err.code == "cache_miss"
        assert str(err) == "cache_miss"

    def test_error_details(self):
        err = NetworkError("http_error", details={"http_status": 500})
        assert err.details == {"http_status": 500}


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_source_id(self):
        assert SOURCE_ID == "osm_overpass_network"

    def test_adapter_version(self):
        assert ADAPTER_VERSION == "overpass-network-v1"

    def test_endpoint(self):
        assert ENDPOINT == "https://overpass-api.de/api/interpreter"

    def test_max_bytes(self):
        assert MAX_BYTES == 100 * 1024 * 1024

    def test_attribution(self):
        assert ATTRIBUTION == "© OpenStreetMap contributors (ODbL)"
