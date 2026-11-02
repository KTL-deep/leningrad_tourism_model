"""Synthetic offline/locality tests; no external HTTP."""

from datetime import datetime, timedelta, timezone
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from src.data_models.study_area import StudyArea
from src.etl import heritage_locality as locality
from src.etl.adapters import nominatim_boundary as boundary
from src.storage import SnapshotStore


POLYGON = {"type": "Polygon", "coordinates": [[[29, 59], [30, 59], [30, 60], [29, 59]]]}


def record(**changes):
    value = {"osm_type": "relation", "osm_id": 12, "category": "boundary", "type": "administrative",
             "name": "Петергоф", "display_name": "Петергоф", "addresstype": "city",
             "address": {"city": "Петергоф", "state": "Санкт-Петербург", "country_code": "ru"},
             "geojson": POLYGON}
    value.update(changes)
    return value


class Response:
    def __init__(self, records, status=200):
        self.body = json.dumps(records, ensure_ascii=False).encode()
        self.status_code = status
        self.headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def iter_content(self, chunk_size):
        yield self.body


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        return self.response


@pytest.fixture
def setup(tmp_path, monkeypatch):
    clock = {"now": datetime(2026, 10, 26, tzinfo=timezone.utc)}
    monkeypatch.setattr(boundary, "_now", lambda: clock["now"])
    session = Session(Response([record()]))
    store = SnapshotStore(tmp_path / "store")
    cache = tmp_path / "cache"
    area = StudyArea("target", {"type": "Polygon", "coordinates": [[[30, 60], [31, 60], [31, 61], [30, 60]]]})
    return locality.HeritageLocalityResolver(store, cache, session), area, store, cache, session, clock


@pytest.mark.parametrize("address,region,expected", [
    ("обл., г.Гатчина, ул. Ленина", "Ленинградская область", "Гатчина"),
    ("г. Санкт-Петербург, г. Петергоф, улица", "г. Санкт-Петербург", "Петергоф"),
    ("город Санкт-Петербург, дом 3", "Санкт-Петербург", "Санкт-Петербург"),
    ("г. Йошкар-Ола, ул. Мира", "Марий Эл", "Йошкар-Ола"),
    ("г. München, дом 1", "Регион", "München"),
])
def test_explicit_city(address, region, expected):
    parsed = locality.extract_address_locality(address, region)
    normalized_region = region.removeprefix("г. ")
    assert parsed == {"locality_name": expected, "region_name": normalized_region,
                      "query": f"{expected}, {normalized_region}, Россия"}


@pytest.mark.parametrize("address", ["ул. Гатчинская, 1", "г. Гатчина, г. Выборг", "г. Санкт-Петербург, г. Петергоф, г. Кронштадт",
                                      "г. Гатчина; г. Петергоф", "г. Москва\nулица", "г. Гатчина!", "г. ", "г. Гатчина" + " " * 8192, 12, ""])
def test_no_inferred_or_unsafe_city(address):
    assert locality.extract_address_locality(address, "Санкт-Петербург") is None


def test_rebound_preserves_original_snapshot_and_offline_cache(setup):
    resolver, area, store, cache, session, clock = setup
    station = record(osm_id=13, category="railway", type="station", geojson={"type": "Point", "coordinates": [29, 59]})
    session.response = Response([station, record(), record()])
    first = resolver.resolve(area, "Петергоф", "Санкт-Петербург")
    context = locality.validate_locality_context(first.context)
    assert context == first.context and context is not first.context
    assert first.snapshot.area_id == area.area_id and first.snapshot.completeness == "partial"
    assert first.snapshot.source_id == locality.SOURCE_ID and first.snapshot.parser_version == locality.PARSER_VERSION
    original = store.get_snapshot(context["original_snapshot_id"])
    assert original.area_id != area.area_id and original.completeness == "complete"
    assert original.content_hash == first.snapshot.content_hash == context["raw_sha256"]
    assert store.read_content(original.snapshot_id) == store.read_content(first.snapshot.snapshot_id)
    assert original.retrieved_at == first.snapshot.retrieved_at == first.checked_at == clock["now"]
    assert context["bbox"] == [29.0, 59.0, 30.0, 60.0]
    offline = locality.HeritageLocalityResolver(store, cache, Session(AssertionError("network")), offline=True)
    clock["now"] += timedelta(hours=1)
    second = offline.resolve(area, "Петергоф", "Санкт-Петербург")
    assert second.from_cache and second.checked_at == first.checked_at and second.snapshot == first.snapshot
    assert session.calls == 1


@pytest.mark.parametrize("records,code", [
    ([record(name="Выборг", address={"city": "Выборг", "state": "Санкт-Петербург", "country_code": "ru"})], "wrong_locality"),
    ([record(address={"city": "Петергоф", "state": "Ленинградская область", "country_code": "ru"})], "wrong_region"),
    ([record(addresstype="county")], "wrong_locality_type"),
    ([record(address={"city": "Петергоф", "state": "Санкт-Петербург"})], "wrong_region"),
    ([record(), record(osm_id=18)], "ambiguous_boundary"),
    ([record(geojson={"type": "Point", "coordinates": [29, 59]})], "not_found"),
])
def test_rejects_unverified_locality(setup, records, code):
    resolver, area, store, _, session, _ = setup
    session.response = Response(records)
    with pytest.raises(locality.LocalityError) as error:
        resolver.resolve(area, "Петергоф", "Санкт-Петербург")
    assert error.value.code == code
    assert not any(store.get_snapshot(path.stem).source_id == locality.SOURCE_ID
                   for path in (store.root / "snapshots").glob("*.json"))


@pytest.mark.parametrize("status,code", [(403, "access_denied"), (429, "rate_limited")])
def test_denial_propagates(setup, status, code):
    resolver, area, _, _, session, _ = setup
    session.response = Response([], status=status)
    with pytest.raises(locality.LocalityError) as error:
        resolver.resolve(area, "Петергоф", "Санкт-Петербург")
    assert error.value.code == code


def test_corrupt_cached_blob_fails_closed(setup):
    resolver, area, store, _, _, _ = setup
    result = resolver.resolve(area, "Петергоф", "Санкт-Петербург")
    (store.root / "blobs" / (result.snapshot.content_hash + ".bin")).write_bytes(b"tampered")
    offline = locality.HeritageLocalityResolver(store, resolver.adapter.cache_dir, offline=True)
    with pytest.raises(locality.LocalityError) as error:
        offline.resolve(area, "Петергоф", "Санкт-Петербург")
    assert error.value.code == "invalid_cache"


def test_polygon_must_equal_adapter_geometry_and_not_response_bbox(setup, monkeypatch):
    resolver, area, store, _, session, _ = setup
    session.response = Response([record(boundingbox=[-90, 90, -180, 180])])
    assert resolver.resolve(area, "Петергоф", "Санкт-Петербург").context["bbox"] == [29.0, 59.0, 30.0, 60.0]
    collected = resolver.adapter.collect("Петергоф, Санкт-Петербург, Россия")
    different = StudyArea("other", {"type": "Polygon", "coordinates": [[[28, 58], [29, 58], [29, 59], [28, 58]]]})
    monkeypatch.setattr(resolver.adapter, "collect", lambda _: replace(
        collected, study_area=SimpleNamespace(area_id=collected.study_area.area_id,
                                               request_geometry=different.request_geometry)))
    with pytest.raises(locality.LocalityError) as error:
        resolver.resolve(area, "Петергоф", "Санкт-Петербург")
    assert error.value.code == "geometry_mismatch"


@pytest.mark.parametrize("mutate", [
    lambda text: text.replace('"country_code": "ru"', '"country_code": "us", "country_code": "ru"'),
    lambda text: text.replace('"geojson": {', '"geojson": {"type": "Point", "coordinates": [0, 0]}, "geojson": {'),
    lambda text: text.replace('"osm_id": 12', '"boundingbox": [1e999, 0, 0, 0], "osm_id": 12'),
    lambda text: text.replace('"osm_id": 12', '"boundingbox": [NaN, 0, 0, 0], "osm_id": 12'),
])
def test_strict_raw_json_rejects_adapter_accepted_response(setup, mutate):
    resolver, area, store, _, session, _ = setup
    session.response.body = mutate(session.response.body.decode()).encode()
    with pytest.raises(locality.LocalityError) as error:
        resolver.resolve(area, "Петергоф", "Санкт-Петербург")
    assert error.value.code == "invalid_response"
    assert not any(store.get_snapshot(path.stem).source_id == locality.SOURCE_ID
                   for path in (store.root / "snapshots").glob("*.json"))


@pytest.mark.parametrize("change", [
    {"byte_length": boundary.MAX_BYTES + 1}, {"source_id": "other"},
    {"parser_version": "other"}, {"area_id": "other"},
])
def test_snapshot_metadata_verified_before_read(setup, monkeypatch, change):
    resolver, area, store, _, _, _ = setup
    collected = resolver.adapter.collect("Петергоф, Санкт-Петербург, Россия")
    monkeypatch.setattr(resolver.adapter, "collect", lambda _: collected)
    original_get = store.get_snapshot
    monkeypatch.setattr(store, "get_snapshot", lambda _: replace(original_get(collected.snapshot.snapshot_id), **change))
    monkeypatch.setattr(store, "read_content", lambda _: pytest.fail("metadata must be checked before read"))
    with pytest.raises(locality.LocalityError) as error:
        resolver.resolve(area, "Петергоф", "Санкт-Петербург")
    assert error.value.code == "invalid_snapshot"


def test_collection_snapshot_must_match_verified_metadata(setup, monkeypatch):
    resolver, area, _, _, _, _ = setup
    collected = resolver.adapter.collect("Петергоф, Санкт-Петербург, Россия")
    monkeypatch.setattr(resolver.adapter, "collect", lambda _: replace(
        collected, snapshot=replace(collected.snapshot, source_id="wrong")))
    with pytest.raises(locality.LocalityError) as error:
        resolver.resolve(area, "Петергоф", "Санкт-Петербург")
    assert error.value.code == "invalid_snapshot"


def test_length_limits_without_network(setup):
    resolver, area, _, _, session, _ = setup
    for address, region in [("г. " + "А" * 257, "Регион"), ("г. Гатчина", "Р" * 513)]:
        assert locality.extract_address_locality(address, region) is None
    for name, region in [("А" * 257, "Регион"), ("Петергоф", "Р" * 513)]:
        with pytest.raises(locality.LocalityError) as error:
            resolver.resolve(area, name, region)
        assert error.value.code == "invalid_locality"
    assert session.calls == 0


def test_guarded_envelope_and_invalid_inputs(setup):
    context = setup[0].resolve(setup[1], "Петергоф", "Санкт-Петербург").context
    compare = locality.compare_locality_context
    assert compare(context, (30.01, 59.5))["status"] == "inside_or_near_context_envelope"
    assert compare(context, (31, 59.5))["status"] == "outside_guarded_context_envelope"
    assert compare(context, None)["status"] == "unknown_geometry"
    for point in ((True, 59), (float("nan"), 59), (181, 59), {"lon": 1}):
        with pytest.raises(locality.LocalityError):
            compare(context, point)
    for bbox in ([0, 84.99, 1, 85.01], [179, 59, 180, 60], [-180, 59, 180, 60]):
        high = {**context, "bbox": bbox}
        assert compare(high, (0, 60))["status"] == "unknown_geometry"
    for bbox in ([1, 2, 1, 3], [True, 2, 3, 4], [1, 2, float("nan"), 4]):
        with pytest.raises(locality.LocalityError):
            compare({**context, "bbox": bbox}, None)
