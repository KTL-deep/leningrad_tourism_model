"""Small synthetic MKRF numbered JSON ZIP archives (no national downloads)."""

import io
import json
import warnings
import zipfile

import pytest

from src.etl.adapters import mkrf_registry as mkrf
from tests.test_mkrf_registry import AREA, DATASET_ID, SCHEMA, Response, Session, row
from src.storage import SnapshotStore


def zipped(*members):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in members:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                archive.writestr(name, body)
    return buffer.getvalue()


def encoded(*rows):
    return json.dumps(rows, ensure_ascii=False).encode()


def test_multimember_global_identity_and_conflict(tmp_path):
    first = row(number="123", native="one")
    second = row(number="123", native="two")
    second["_id"] = "second-mongo"
    path = tmp_path / "json.zip"
    path.write_bytes(zipped(("2.json", encoded(second)), ("1.json", encoded(first, row("56")))))
    records, scanned = mkrf.parse_json_archive(path, AREA)
    assert scanned == 3
    assert [r["mongo_id"] for r in records] == [first["_id"], second["_id"]]
    assert all(r["registry_number_conflict"] for r in records)
    path.write_bytes(zipped(("1.json", encoded(first)), ("2.json", encoded(first))))
    with pytest.raises(mkrf.MkrfError, match="duplicate_native_id"):
        mkrf.parse_json_archive(path, AREA)


@pytest.mark.parametrize("members,code", [
    ([("2.json", b"[]")], "unsafe_archive"),
    ([("1.json", b"[]"), ("1.json", b"[]")], "unsafe_archive"),
    ([("../1.json", b"[]")], "unsafe_archive"),
    ([("1.json", b"[],")], "invalid_array"),
    ([("1.json", b"[")], "invalid_array"),
    ([("1.json", b"[1]")], "invalid_array"),
    ([("1.json", b"[]{}")], "invalid_array"),
    ([("1.json", b"[{\"a\":1,\"a\":2}]")], "duplicate_json_key"),
    ([("1.json", b"[{\"a\":NaN}]")], "nonfinite_json"),
    ([("1.json", b"[\xff]")], "invalid_archive"),
])
def test_invalid_arrays_and_names(tmp_path, members, code):
    path = tmp_path / "bad.zip"
    path.write_bytes(zipped(*members))
    with pytest.raises(mkrf.MkrfError, match=code):
        mkrf.parse_json_archive(path, AREA)


def test_crc_size_truncation_and_limits(tmp_path, monkeypatch):
    path = tmp_path / "bad.zip"
    path.write_bytes(zipped(("1.json", encoded(row()))))
    damaged = bytearray(path.read_bytes())
    central = damaged.index(b"PK\x01\x02")
    damaged[central + 16] ^= 1
    path.write_bytes(damaged)
    with pytest.raises(mkrf.MkrfError, match="invalid_archive"):
        mkrf.parse_json_archive(path, AREA)
    path.write_bytes(zipped(("1.json", encoded(row()))))
    damaged = bytearray(path.read_bytes())
    central = damaged.index(b"PK\x01\x02")
    damaged[central + 24] ^= 1
    path.write_bytes(damaged)
    with pytest.raises(mkrf.MkrfError, match="invalid_archive|archive_size_mismatch"):
        mkrf.parse_json_archive(path, AREA)
    path.write_bytes(path.read_bytes()[:-15])
    with pytest.raises(mkrf.MkrfError, match="invalid_archive"):
        mkrf.parse_json_archive(path, AREA)
    path.write_bytes(zipped(("1.json", encoded(row()))))
    monkeypatch.setattr(mkrf, "MAX_UNCOMPRESSED", 5)
    with pytest.raises(mkrf.MkrfError, match="unsafe_archive"):
        mkrf.parse_json_archive(path, AREA)
    monkeypatch.setattr(mkrf, "MAX_UNCOMPRESSED", 2 * 1024 * 1024 * 1024)
    monkeypatch.setattr(mkrf, "MAX_LINE", 10)
    with pytest.raises(mkrf.MkrfError, match="archive_limit"):
        mkrf.parse_json_archive(path, AREA)


def test_chunk_boundary_record_limits_and_deadline(tmp_path, monkeypatch):
    path = tmp_path / "ok.zip"
    record = row()
    record["data"]["general"]["name"] = "Ж" * 40000
    path.write_bytes(zipped(("1.json", encoded(record))))
    assert mkrf.parse_json_archive(path, AREA)[1] == 1
    monkeypatch.setattr(mkrf, "MAX_RECORDS", 1)
    path.write_bytes(zipped(("1.json", encoded(row())), ("2.json", encoded(row("56")))))
    with pytest.raises(mkrf.MkrfError, match="archive_limit"):
        mkrf.parse_json_archive(path, AREA)
    monkeypatch.setattr(mkrf, "MAX_RECORDS", 500000)
    monkeypatch.setattr(mkrf, "MAX_RETAINED", 1)
    path.write_bytes(zipped(("1.json", encoded(row())),
                            ("2.json", encoded(row(number="456", native="n2")))))
    with pytest.raises(mkrf.MkrfError, match="retained_limit"):
        mkrf.parse_json_archive(path, AREA)
    monkeypatch.setattr(mkrf, "DEADLINE", -1)
    with pytest.raises(mkrf.MkrfError, match="parser_deadline"):
        mkrf.parse_json_archive(path, AREA)


def test_official_formats_fallback_and_offline(tmp_path):
    base = mkrf.SOURCE_URL
    passport = (f"<input id='dataset_id' value='{DATASET_ID}'>" + "".join(
        f"<div class='download_btn' data-url='/opendata/7705851331-egrkn/' "
        f"data-name='data-70-structure-{n}' data-formats='json.zip,jsons.zip,csv.zip'></div>"
        f"<div class='download_btn' data-url='/opendata/7705851331-egrkn/' "
        f"data-name='structure-{n}' data-formats='json'></div>" for n in (17, 16))).encode()
    payload = zipped(("1.json", encoded(row())), ("2.json", encoded(row("78", number="456", native="n2"))))
    urls = [base + f"data-70-structure-{n}.{fmt}" for n in (17, 16)
            for fmt in ("json.zip", "jsons.zip")]
    session = Session({base: Response(base, 200, passport, "text/html"),
                       urls[0]: Response(urls[0], 404, b"", "text/html"),
                       urls[1]: Response(urls[1], 410, b"", "text/html"),
                       urls[2]: Response(urls[2], 200, payload, "application/zip"),
                       base + "structure-16.json": Response(base + "structure-16.json", 200,
                                                             json.dumps(SCHEMA).encode(), "application/json")})
    store = SnapshotStore(tmp_path / "store")
    result = mkrf.MkrfRegistryAdapter(store, tmp_path / "cache", session).collect(AREA)
    assert session.calls == [base, *urls[:3], base + "structure-16.json"]
    assert result.registry["source_versions"]["archive_format"] == "json.zip"
    assert result.registry["source_versions"]["archive_structure"] == 16
    assert result.registry["quality"]["scanned_records"] == 2
    assert mkrf.MkrfRegistryAdapter(store, tmp_path / "cache", offline=True).collect(AREA).registry == result.registry


def test_invalid_json_archive_never_falls_back(tmp_path):
    base = mkrf.SOURCE_URL
    passport = (f"<input id='dataset_id' value='{DATASET_ID}'>"
                "<div class='download_btn' data-url='/opendata/7705851331-egrkn/' "
                "data-name='data-70-structure-16' data-formats='json.zip,jsons.zip'></div>"
                "<div class='download_btn' data-url='/opendata/7705851331-egrkn/' "
                "data-name='structure-16' data-formats='json'></div>").encode()
    url = base + "data-70-structure-16.json.zip"
    session = Session({base: Response(base, 200, passport, "text/html"),
                       url: Response(url, 200, zipped(("1.json", b"[broken]")), "application/zip"),
                       base + "structure-16.json": Response(base + "structure-16.json", 200,
                                                             json.dumps(SCHEMA).encode(), "application/json")})
    with pytest.raises(mkrf.MkrfError, match="invalid_array"):
        mkrf.MkrfRegistryAdapter(SnapshotStore(tmp_path / "store"), tmp_path / "cache", session).collect(AREA)
    assert session.calls == [base, url, base + "structure-16.json"]
