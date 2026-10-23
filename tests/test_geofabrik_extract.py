import json
from datetime import datetime, timedelta, timezone

import requests

import pytest

from src.data_models.study_area import StudyArea
from src.etl.adapters.geofabrik_extract import (ExtractError, GeofabrikExtractAdapter,
                                                POLY_URL, URL, coverage_polygon)
from src.etl.adapters import geofabrik_extract
from src.etl.osm_extract_parsing import extract_layers


POLY = b"NW\n1\n 29 59\n 32 59\n 32 62\n 29 62\nEND\nEND\n"


def area(x=30):
    return StudyArea("sample", {"type": "Polygon", "coordinates": [
        [[x, 60], [x + .01, 60], [x + .01, 60.01], [x, 60.01], [x, 60]]]})


class Response:
    status_code = 200

    def __init__(self, content, *, status=200, headers=None):
        self.content = content
        self.status_code = status
        self.headers = headers if headers is not None else {"Content-Length": str(len(content))}
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def close(self):
        self.closed = True

    def iter_content(self, chunk_size):
        yield self.content


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.urls = []

    def get(self, url, **kwargs):
        assert kwargs["allow_redirects"] is False and kwargs["stream"] and kwargs["verify"]
        self.urls.append(url)
        return next(self.responses)


def test_coverage_and_rejection(tmp_path):
    session = Session([Response(POLY)])
    adapter = GeofabrikExtractAdapter(tmp_path, session=session)
    assert coverage_polygon(POLY).covers(coverage_polygon(POLY))
    with pytest.raises(ExtractError, match="outside_extract_coverage"):
        adapter.collect(area(35))
    assert session.urls == [POLY_URL]


def test_hole_and_bbox_limit(tmp_path):
    poly = (b"NW\n1\n29 59\n32 59\n32 62\n29 62\nEND\n!2\n"
            b"30 60\n31 60\n31 61\n30 61\nEND\nEND\n")
    assert not coverage_polygon(poly).covers(coverage_polygon(
        b"inner\n1\n30.1 60.1\n30.2 60.1\n30.2 60.2\n30.1 60.2\nEND\nEND\n"))
    with pytest.raises(Exception, match="bbox_too_large"):
        GeofabrikExtractAdapter(tmp_path, offline=True).collect(StudyArea(
            "huge", {"type": "Polygon", "coordinates": [
                [[30, 60], [32, 60], [32, 62], [30, 62], [30, 60]]]}))


def test_redirect_cache_and_corruption(tmp_path):
    resolved = "https://download.geofabrik.de/russia/northwestern-fed-district-261021.osm.pbf"
    session = Session([Response(POLY), Response(b"", status=307, headers={"Location": resolved}), Response(b"pbf")])
    adapter = GeofabrikExtractAdapter(tmp_path, session=session)
    path, record, cached, _, _ = adapter.collect(area())
    assert record["url"] == URL and record["resolved_url"] == resolved and not cached
    assert adapter.collect(area())[2] is True
    path.write_bytes(b"bad")
    with pytest.raises(ExtractError, match="invalid_cache"):
        adapter.collect(area())


@pytest.mark.parametrize("response, code", [
    (Response(b"", status=403), "http_error"),
    (Response(b"oversize", headers={"Content-Length": str(768 * 1024 * 1024 + 1)}), "invalid_content_length"),
    (Response(b"short", headers={"Content-Length": "10"}), "truncated_response"),
])
def test_download_fail_closed(tmp_path, response, code):
    with pytest.raises(ExtractError, match=code):
        GeofabrikExtractAdapter(tmp_path, session=Session([response]))._get("extract", URL, 768 * 1024 * 1024)


def test_unsafe_redirect(tmp_path):
    response = Response(b"", status=307, headers={"Location": "https://evil.example/file"})
    session = Session([response])
    with pytest.raises(ExtractError, match="unsafe_redirect"):
        GeofabrikExtractAdapter(tmp_path, session=session)._get("extract", URL, 768 * 1024 * 1024)
    assert response.closed


def test_stream_limit(tmp_path):
    with pytest.raises(ExtractError, match="response_too_large"):
        GeofabrikExtractAdapter(tmp_path, session=Session([Response(b"12345", headers={})]))._get(
            "coverage.poly", POLY_URL, 4)


def test_offline_missing_and_expired(tmp_path):
    adapter = GeofabrikExtractAdapter(tmp_path, offline=True)
    with pytest.raises(ExtractError, match="cache_miss"):
        adapter._get("coverage.poly", POLY_URL, 1024)
    online = GeofabrikExtractAdapter(tmp_path, session=Session([Response(POLY)]))
    online._get("coverage.poly", POLY_URL, 1024)
    manifest = tmp_path / "coverage.poly.json"
    data = json.loads(manifest.read_text())
    data["received_at"] = "2020-01-01T00:00:00+00:00"
    manifest.write_text(json.dumps(data))
    with pytest.raises(ExtractError, match="cache_expired"):
        adapter._get("coverage.poly", POLY_URL, 1024)


@pytest.mark.parametrize("field, value", [
    ("received_at", "2999-01-01T00:00:00+00:00"),
    ("received_at", "2026-01-01T00:00:00"),
    ("received_at", 17),
    ("ttl_seconds", True),
    ("size", True),
    ("sha256", "../bad"),
    ("resolved_url", "https://elsewhere.example/pbf"),
])
def test_invalid_manifest_values_fail_closed(tmp_path, field, value):
    online = GeofabrikExtractAdapter(tmp_path, session=Session([Response(POLY)]))
    online._get("coverage.poly", POLY_URL, 1024)
    manifest = tmp_path / "coverage.poly.json"
    record = json.loads(manifest.read_text())
    record[field] = value
    manifest.write_text(json.dumps(record))
    with pytest.raises(ExtractError, match="invalid_cache"):
        GeofabrikExtractAdapter(tmp_path, offline=True)._get("coverage.poly", POLY_URL, 1024)


def test_failed_pointer_publication_preserves_old_cache(tmp_path, monkeypatch):
    online = GeofabrikExtractAdapter(tmp_path, session=Session([Response(b"old"), Response(b"new")]))
    old_path, old_record, _ = online._get("extract", URL, 1024)
    pointer = tmp_path / "extract.json"
    expired = {**old_record, "received_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()}
    pointer.write_text(json.dumps(expired))
    replace = geofabrik_extract.os.replace

    def fail_pointer(source, target):
        if target == pointer:
            raise OSError("simulated pointer publication failure")
        return replace(source, target)

    monkeypatch.setattr(geofabrik_extract.os, "replace", fail_pointer)
    with pytest.raises(OSError, match="simulated"):
        online._get("extract", URL, 1024)
    assert json.loads(pointer.read_text()) == expired
    assert old_path.read_bytes() == b"old"
    with pytest.raises(ExtractError, match="cache_expired"):
        GeofabrikExtractAdapter(tmp_path, offline=True)._get("extract", URL, 1024)
    pointer.write_text(json.dumps(old_record))
    assert GeofabrikExtractAdapter(tmp_path, offline=True)._get("extract", URL, 1024)[0] == old_path


def test_collect_busy_and_ownership(tmp_path):
    adapter = GeofabrikExtractAdapter(tmp_path, offline=True)
    lock = tmp_path / "collect.lock"
    lock.write_text("other-process")
    with pytest.raises(ExtractError, match="cache_busy"):
        adapter.collect(area())
    assert lock.read_text() == "other-process"
    lock.unlink()

    class ReplacedLock(GeofabrikExtractAdapter):
        def _get(self, *args):
            lock.unlink()
            lock.write_text("new-owner")
            raise ExtractError("test_error")

    with pytest.raises(ExtractError, match="test_error"):
        ReplacedLock(tmp_path, offline=True).collect(area())
    assert lock.read_text() == "new-owner"


@pytest.mark.parametrize("error, code", [
    (requests.exceptions.SSLError("ssl"), "tls_error"),
    (requests.exceptions.Timeout("timeout"), "timeout"),
])
def test_network_failures(tmp_path, error, code):
    class FailingSession:
        def get(self, *args, **kwargs):
            raise error

    with pytest.raises(ExtractError, match=code):
        GeofabrikExtractAdapter(tmp_path, session=FailingSession())._get("extract", URL, 1024)


def test_offline_is_strict_bool(tmp_path):
    with pytest.raises(ValueError):
        GeofabrikExtractAdapter(tmp_path, offline=1)


def test_small_real_pbf(tmp_path):
    osmium = pytest.importorskip("osmium")
    path = tmp_path / "sample.osm.pbf"
    with osmium.SimpleWriter(str(path)) as writer:
        writer.add_node(osmium.osm.mutable.Node(id=1, location=osmium.osm.Location(30.001, 60.001),
                                                tags={"tourism": "museum"}))
        writer.add_node(osmium.osm.mutable.Node(id=2, location=osmium.osm.Location(30.02, 60.02)))
        writer.add_way(osmium.osm.mutable.Way(id=3, nodes=[1, 2], tags={"highway": "residential"}))
    paths, counts, _ = extract_layers(path, area(), tmp_path / "features")
    assert counts["poi"] == counts["network"] == 1
    road = json.loads(paths["network"].read_text().splitlines()[0])
    assert road["properties"]["node_ids"] == [1, 2]
    assert road["properties"]["extends_request"] is True
