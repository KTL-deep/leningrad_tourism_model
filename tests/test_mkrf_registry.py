"""Synthetic offline fixtures shaped like MKRF's public JSON-lines ZIP export."""

from datetime import datetime, timedelta, timezone
import io
import json
import zipfile

import pytest

from src.data_models.study_area import StudyArea
from src.etl.adapters.mkrf_registry import (MkrfError, MkrfRegistryAdapter, SOURCE_URL,
                                            DATASET_ID, MAX_COMPRESSED, MAX_LINE, discover, parse_archive,
                                            validate_schema, _fetch)
from src.storage import SnapshotStore


AREA = StudyArea("Gatchina", {"type": "Polygon", "coordinates": [
    [[30, 59], [31, 59], [31, 60], [30, 60], [30, 59]]]},
    jurisdictions=("Ленинградская область",))


def passport(extra=""):
    return ("<input id='dataset_id' type='hidden' value='" + DATASET_ID + "'>"
            "<div class='download_btn' data-url='/opendata/7705851331-egrkn/' "
            "data-name='data-70-structure-17' data-formats='jsons.zip,csv.zip'></div>"
            "<div class='download_btn' data-url='/opendata/7705851331-egrkn/' "
            "data-name='data-70-structure-16' data-formats='jsons.zip'></div>"
            "<div class='download_btn' data-url='/opendata/7705851331-egrkn/' "
            "data-name='structure-17' data-formats='json'></div>"
            "<div class='download_btn' data-url='/opendata/7705851331-egrkn/' "
            "data-name='structure-16' data-formats='json'></div>"
            "<div>26.10.2026</div>" + extra).encode()


def row(region="47", coords=(30.5, 59.5), number="123", native="n1"):
    address = {"fullAddress": "Адрес"}
    if coords is not None:
        address["mapPosition"] = {"type": "Point", "coordinates": list(coords)}
    return {"_id": "mongo-" + number, "odSchema": "69c2a94f3146bb0ca41fd74a",
            "dataset": DATASET_ID, "odSetVersion": "6ad0bc50ac470510e3bb3a5b",
            "odSetVersions": ["6ad0bc50ac470510e3bb3a5b"],
            "nativeId": native, "status": 2, "data": {"general": {
                "id": "g-" + number, "regNumber": number, "name": "Памятник " + number,
                "region": {"id": region, "value": "Область"}, "address": address,
                "isActual": True, "categoryType": {"value": "local"},
                "objectType": {"value": "building"}, "status": "active", "createDate": "2026-01-01"}}}


def archive(rows=None, member="data-70-structure-16.jsons"):
    if rows is None:
        rows = [row(), row("78", (31.5, 59.5), "456", "n2"),
                row("47", None, "789", "n3"), row("56", (55, 51), "999", "n4")]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as file:
        file.writestr(member, b"".join(json.dumps(r, ensure_ascii=False).encode() + b"\n" for r in rows))
    return buf.getvalue()


SCHEMA = {"type": "object", "properties": {"general": {"type": "object", "properties": {
    "name": {"type": "string"}, "regNumber": {"type": "string"},
    "region": {"type": "object", "properties": {"id": {"type": "string"}}},
    "address": {"type": "object", "properties": {"mapPosition": {
        "type": "object", "logicalType": "GeoJSON", "format": "lat,long",
        "properties": {"type": {"type": "string"}, "coordinates": {"type": "array"}}}}}}}}}


class Response:
    def __init__(self, url, status, body, media):
        self.url, self.status_code, self.body = url, status, body
        self.headers = {"Content-Type": media, "Content-Length": str(len(body))}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset:offset + chunk_size]


class Session:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def get(self, url, **kwargs):
        assert kwargs["verify"] is True and kwargs["allow_redirects"] is False
        assert kwargs["stream"] is True and kwargs["headers"] == {"User-Agent": kwargs["headers"]["User-Agent"]}
        assert "Authorization" not in kwargs["headers"] and kwargs["timeout"] == (10, 60)
        self.calls.append(url)
        return self.responses[url]


def setup(tmp_path, *, status=404, rows=None):
    base = SOURCE_URL
    session = Session({base: Response(base, 200, passport(), "text/html"),
                       base + "data-70-structure-17.jsons.zip": Response(base + "data-70-structure-17.jsons.zip", status, b"", "text/html"),
                       base + "data-70-structure-16.jsons.zip": Response(base + "data-70-structure-16.jsons.zip", 200, archive(rows), "application/zip"),
                       base + "structure-16.json": Response(base + "structure-16.json", 200, json.dumps(SCHEMA).encode(), "application/json")})
    store = SnapshotStore(tmp_path / "store")
    return MkrfRegistryAdapter(store, tmp_path / "cache", session), session, store


def test_collect_fallback_real_shape_provenance_and_offline_cache(tmp_path):
    adapter, session, store = setup(tmp_path)
    result = adapter.collect(AREA)
    assert session.calls == [SOURCE_URL, SOURCE_URL + "data-70-structure-17.jsons.zip",
                             SOURCE_URL + "data-70-structure-16.jsons.zip", SOURCE_URL + "structure-16.json"]
    assert result.registry["quality"] == {"scanned_records": 4, "retained_records": 3,
                                          "unlocated": 1, "outside_analysis": 1,
                                          "observed_source_schema_ids": ["69c2a94f3146bb0ca41fd74a"],
                                          "duplicate_registry_numbers": 0}
    assert result.registry["source_versions"]["archive_structure"] == 16
    assert result.schema_snapshot.url.endswith("structure-16.json")
    assert result.archive_snapshot.byte_length == len(archive())
    assert result.registry["records"][0]["spatial_status"] == "intersects_analysis"
    assert result.registry["records"][0]["reg_number"] == "123"
    assert result.registry["records"][0]["native_id"] == "n1"
    assert result.registry["records"][0]["source_record_id"] == "mkrf_egrkn:mongo-123"
    assert result.registry["records"][0]["source_schema_id"] == "69c2a94f3146bb0ca41fd74a"
    assert result.registry["schema_numeric_to_source_id_mapping"] == "unresolved"
    assert result.registry["records"][0]["registry_number_status"] == "invalid"
    assert result.registry["source_reported_date"] is None
    assert result.registry["records"][2]["location_reason"] == "missing_point"
    assert "longitude, latitude" in result.registry["coordinate_order_warning"]
    assert store.read_content(result.archive_snapshot.snapshot_id) == archive()
    offline = MkrfRegistryAdapter(store, tmp_path / "cache", offline=True).collect(AREA)
    assert offline.from_cache and offline.registry == result.registry
    assert offline.archive_snapshot.snapshot_id == result.archive_snapshot.snapshot_id
    assert adapter.collect(AREA).from_cache and len(session.calls) == 4


@pytest.mark.parametrize("status", [403, 429, 500])
def test_no_fallback_on_nonmissing_status(tmp_path, status):
    adapter, session, _ = setup(tmp_path, status=status)
    with pytest.raises(MkrfError, match="http_" + str(status)):
        adapter.collect(AREA)
    assert len(session.calls) == 2


@pytest.mark.parametrize("extra", [
    "<div class='download_btn' data-url='https://evil.example/' data-name='data-71-structure-18' data-formats='jsons.zip'></div>",
    "<div class='download_btn' data-url='/opendata/7705851331-egrkn/' data-name='data-71-structure-../evil' data-formats='jsons.zip'></div>",
    "<div class='download_btn' data-url='/opendata/7705851331-egrkn/' data-name='data-71-structure-18' data-formats='jsons.zip'></div>",
])
def test_unsafe_or_missing_schema_advertisement(extra):
    with pytest.raises(MkrfError):
        discover(passport(extra))


def test_passport_dataset_uid_and_record_namespace_fail_closed(tmp_path):
    with pytest.raises(MkrfError, match="dataset_identity_mismatch"):
        discover(passport().replace(DATASET_ID.encode(), b"000000000000000000000000"))
    path = tmp_path / "bad.zip"
    for bad, code in ((dict(row(), dataset="other"), "dataset_identity_mismatch"),
                      (dict(row(), odSchema="structure-16"), "invalid_source_schema_id")):
        path.write_bytes(archive([bad]))
        with pytest.raises(MkrfError, match=code):
            parse_archive(path, "data-70-structure-16.jsons", AREA)


def test_archive_limits_and_duplicates(tmp_path, monkeypatch):
    path = tmp_path / "fixture.zip"
    path.write_bytes(archive(member="../data-70-structure-16.jsons"))
    with pytest.raises(MkrfError, match="unsafe_archive"):
        parse_archive(path, "../data-70-structure-16.jsons", AREA)
    path.write_bytes(archive([row(), row(number="123", native="other")]))
    with pytest.raises(MkrfError, match="duplicate_mongo_id"):
        parse_archive(path, "data-70-structure-16.jsons", AREA)
    path.write_bytes(archive([row()]))
    monkeypatch.setattr("src.etl.adapters.mkrf_registry.MAX_COMPRESSED", 5)
    with pytest.raises(MkrfError, match="oversized_archive"):
        parse_archive(path, "data-70-structure-16.jsons", AREA)
    monkeypatch.setattr("src.etl.adapters.mkrf_registry.MAX_COMPRESSED", MAX_COMPRESSED)
    monkeypatch.setattr("src.etl.adapters.mkrf_registry.MAX_LINE", 12)
    with pytest.raises(MkrfError, match="archive_limit"):
        parse_archive(path, "data-70-structure-16.jsons", AREA)


def test_malformed_geometry_and_json_fail_closed(tmp_path):
    path = tmp_path / "fixture.zip"
    path.write_bytes(archive([row(coords=(200, 59)), row(number="2", native="n2", coords=(30, 59))]))
    records, scanned = parse_archive(path, "data-70-structure-16.jsons", AREA)
    assert scanned == 2 and records[0]["spatial_status"] == "unlocated"
    assert records[0]["location_reason"] == "malformed_point"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as file:
        file.writestr("data-70-structure-16.jsons", b'{"data":{},"data":{}}\n')
    path.write_bytes(buf.getvalue())
    with pytest.raises(MkrfError, match="duplicate_json_key"):
        parse_archive(path, "data-70-structure-16.jsons", AREA)


def test_crc_extra_member_nonfinite_and_response_guards(tmp_path):
    path = tmp_path / "fixture.zip"
    path.write_bytes(archive([row()]))
    damaged = bytearray(path.read_bytes())
    with zipfile.ZipFile(path) as file:
        info = file.infolist()[0]
    # Alter the CRC in the central directory, retaining a valid ZIP structure.
    central = damaged.index(b"PK\x01\x02")
    damaged[central + 16] ^= 1
    path.write_bytes(damaged)
    with pytest.raises(MkrfError, match="invalid_archive"):
        parse_archive(path, info.filename, AREA)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as file:
        file.writestr(info.filename, b'{}\n')
        file.writestr("extra", b"x")
    path.write_bytes(buf.getvalue())
    with pytest.raises(MkrfError, match="unsafe_archive"):
        parse_archive(path, info.filename, AREA)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as file:
        file.writestr(info.filename, b'{"value":NaN}\n')
    path.write_bytes(buf.getvalue())
    with pytest.raises(MkrfError, match="nonfinite_json"):
        parse_archive(path, info.filename, AREA)

    adapter, session, _ = setup(tmp_path / "fetch")
    first = session.responses[SOURCE_URL]
    first.headers["Content-Length"] = str(len(first.body) + 1)
    with pytest.raises(MkrfError, match="truncated_response"):
        adapter.collect(AREA)
    first.headers["Content-Length"] = str(len(first.body))
    first.url = "https://evil.example/"
    with pytest.raises(MkrfError, match="unsafe_redirect"):
        adapter.collect(AREA)


def test_offline_expired_corrupt_and_publish_failure(tmp_path, monkeypatch):
    adapter, session, store = setup(tmp_path)
    with pytest.raises(MkrfError, match="cache_missing"):
        MkrfRegistryAdapter(store, tmp_path / "cache", offline=True).collect(AREA)
    result = adapter.collect(AREA)
    pointer = next((tmp_path / "cache").glob("*.json"))
    data = json.loads(pointer.read_text())
    checked = datetime.now(timezone.utc) - timedelta(days=2)
    data["checked_at"], data["expires_at"] = checked.isoformat(), (checked + timedelta(days=1)).isoformat()
    pointer.write_text(json.dumps(data))
    with pytest.raises(MkrfError, match="cache_expired"):
        MkrfRegistryAdapter(store, tmp_path / "cache", offline=True).collect(AREA)
    data["checked_at"], data["expires_at"] = datetime.now(timezone.utc).isoformat(), (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    pointer.write_text(json.dumps(data))
    blob = store.root / "blobs" / (result.archive_snapshot.content_hash + ".bin")
    blob.write_bytes(b"corrupt")
    with pytest.raises(MkrfError, match="cache_corrupt"):
        MkrfRegistryAdapter(store, tmp_path / "cache", offline=True).collect(AREA)

    second = tmp_path / "second"
    fresh, _, _ = setup(second)
    def fail(*args):
        raise OSError("publish failed")
    monkeypatch.setattr("src.etl.adapters.mkrf_registry._publish", fail)
    with pytest.raises(OSError, match="publish failed"):
        fresh.collect(AREA)
    assert not list((second / "cache").glob("*.json"))
    assert not list((second / "cache").glob(".mkrf-*"))


def test_registry_number_ambiguity_identity_and_schema(tmp_path):
    rows = [row(number="123456789012345", native="147468"), row(number="123456789012345", native="147469")]
    rows[1]["_id"] = "another-id"
    rows[1]["data"]["general"]["id"] = 147469
    rows.append(row(number="", native="147470"))
    rows.append(row(number="missing", native="147471"))
    rows[-1]["data"]["general"]["regNumber"] = None
    adapter, _, _ = setup(tmp_path, rows=rows)
    result = adapter.collect(AREA).registry
    assert result["quality"]["duplicate_registry_numbers"] == 1
    assert result["quality"]["observed_source_schema_ids"] == ["69c2a94f3146bb0ca41fd74a"]
    assert [r["registry_number_conflict"] for r in result["records"]] == [True, True, False, False]
    assert [r["registry_number_status"] for r in result["records"]] == ["valid_15_digits", "valid_15_digits", "invalid", "missing"]
    assert result["records"][1]["source_record_id"] == "mkrf_egrkn:another-id"
    path = tmp_path / "bad.zip"
    for invalid in (dict(row(), _id=""), dict(row(), nativeId=[]),
                    dict(row(), data={"general": {**row()["data"]["general"], "name": " "}})):
        path.write_bytes(archive([invalid]))
        with pytest.raises(MkrfError, match="invalid_record_identity"):
            parse_archive(path, "data-70-structure-16.jsons", AREA)
    assert validate_schema(SCHEMA) is None
    broken = json.loads(json.dumps(SCHEMA))
    broken["properties"]["general"]["properties"]["address"]["properties"]["mapPosition"]["type"] = "string"
    with pytest.raises(MkrfError, match="unsupported_schema"):
        validate_schema(broken)


def test_expired_online_refresh_and_corrupt_online_fails_closed(tmp_path):
    adapter, session, store = setup(tmp_path)
    old = adapter.collect(AREA)
    pointer = next((tmp_path / "cache").glob("*.json"))
    data = json.loads(pointer.read_text())
    then = datetime.now(timezone.utc) - timedelta(days=2)
    data.update(checked_at=then.isoformat(), expires_at=(then + timedelta(days=1)).isoformat())
    pointer.write_text(json.dumps(data))
    refreshed = adapter.collect(AREA)
    assert not refreshed.from_cache and len(session.calls) == 8
    assert refreshed.archive_snapshot.snapshot_id == old.archive_snapshot.snapshot_id
    assert refreshed.archive_snapshot.retrieved_at == old.archive_snapshot.retrieved_at
    assert refreshed.checked_at > old.checked_at
    blob = store.root / "blobs" / (old.archive_snapshot.content_hash + ".bin")
    blob.write_bytes(b"corrupt")
    with pytest.raises(MkrfError, match="cache_corrupt"):
        adapter.collect(AREA)
    assert len(session.calls) == 8


def test_network_deadline_streamed_chunk(tmp_path, monkeypatch):
    url = SOURCE_URL
    response = Response(url, 200, b"abc", "text/html")
    session = Session({url: response})
    monkeypatch.setattr("src.etl.adapters.mkrf_registry.time.monotonic", lambda: 5)
    with pytest.raises(MkrfError, match="network_deadline"):
        _fetch(session, url, 100, {"text/html"}, deadline=4)
    ticks = iter([1, 1, 1, 3])
    monkeypatch.setattr("src.etl.adapters.mkrf_registry.time.monotonic", lambda: next(ticks))
    with pytest.raises(MkrfError, match="network_deadline"):
        _fetch(session, url, 100, {"text/html"}, deadline=2)
