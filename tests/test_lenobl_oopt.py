"""Offline fixtures patterned on the official OOPT article, not spatial evidence."""

from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest
import requests

from src.data_models.study_area import StudyArea
from src.etl.adapters.lenobl_oopt import (LenoblOoptAdapter, OoptError, SOURCE_URL,
                                          MAX_HTML_BYTES, parse_catalog)
from src.storage import SnapshotStore


AREA = StudyArea("Гатчина", {"type": "Polygon", "coordinates": [
    [[30, 59], [30.1, 59], [30.1, 59.1], [30, 59.1], [30, 59]]]},
    jurisdictions=("Ленинградская область",))


def page(*, first="Государственный природный комплексный заказник «Ореховский»",
         href="orehovskij/", local="Охраняемый природный ландшафт озера Вероярви",
         date="<div class='edit-redactor'>Обновлено 28.01.2026</div>", extra=""):
    return (f"<nav><a href='{href}'>{first}</a></nav>"
            f"<article data-mistake='content'><p>Кадастровые сведения об ООПТ регионального значения:</p>"
            f"<ul><li><em><a href='{href}'>{first}</a></em></li>{extra}</ul>"
            f"<p>Кадастровые сведения об ООПТ метного значения:</p><ul><li>"
            f"<a href='mestnogo-veroyarvi/'>{local}</a></li></ul></article>{date}").encode()


class Response:
    def __init__(self, content, status=200, headers=None, url=SOURCE_URL):
        self.url, self.status_code = url, status
        self.headers = {"Content-Type": "text/html", **(headers or {})}
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, chunk_size):
        yield self.content


class Session:
    def __init__(self, response):
        self.response, self.calls = response, 0

    def get(self, url, **kwargs):
        assert url == SOURCE_URL
        assert kwargs["verify"] is True and kwargs["allow_redirects"] is False
        assert kwargs["stream"] is True and kwargs["timeout"] == (10, 30)
        assert kwargs["headers"]["User-Agent"]
        self.calls += 1
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_catalog_preserves_explicit_names_and_ignores_nav():
    data = page(extra="<li><a href='park/'>Природный парк «Вепсский лес»</a></li>"
                      "<li><a href='monument/'>Памятник природы «Река Величка»</a></li>"
                      "<li><a href='reserve/'>Государственный природный заказник «Кургальский»</a></li>")
    catalog = parse_catalog(data)
    assert len(catalog["items"]) == 5
    assert catalog["source_sha256"] == hashlib.sha256(data).hexdigest()
    assert catalog["source_reported_update"] == "2026-01-28"
    assert catalog["redistribution_license"] == "unknown"
    assert catalog["completeness"] == "partial"
    first = catalog["items"][0]
    assert first["title"] == "Государственный природный комплексный заказник «Ореховский»"
    assert first["name"] == "Ореховский" and first["type"] == "Государственный природный комплексный заказник"
    assert first["level"] == "regional" and first["geometry"] is None
    assert first["location_status"] == "unlocated" and first["legal_effective_status"] == "unknown"
    assert first["source_id"] == "lenobl_oopt_" + hashlib.sha256(first["source_url"].encode()).hexdigest()
    assert catalog["items"][-1]["name"] == "озера Вероярви"
    assert catalog["items"][-1]["level"] == "local"
    assert parse_catalog(page(date=""))["source_reported_update"] is None


@pytest.mark.parametrize("data,code", [
    ("<nav><a href='foo/'>Природный парк «Лес»</a></nav>".encode(), "invalid_article"),
    (b"<article data-mistake='content'></article>", "invalid_article"),
    (page(extra="<li><a href='orehovskij/'>Природный парк «Другой»</a></li>"), "conflicting_child_url"),
    (page(extra="<li><a href='orehovskij/'>Государственный природный комплексный заказник «Ореховский»</a></li>"), "duplicate_child_url"),
    (page(href="https://other.example/path/"), "unsafe_child_url"),
    (page(href="https://user@kpr.lenobl.ru/ru/"), "unsafe_child_url"),
    (page(href="orehovskij/?x=1"), "unsafe_child_url"),
    (page(href="orehovskij/#frag"), "unsafe_child_url"),
    (page(href="https://kpr.lenobl.ru:443/ru/"), "unsafe_child_url"),
    (page(href="%2e%2e/evil/"), "unsafe_child_url"),
    (page(href="../evil/"), "unsafe_child_url"),
    (page(first="Неизвестное имя"), "ambiguous_name_or_type"),
    (b"x" * (MAX_HTML_BYTES + 1), "invalid_html_size"),
], ids=["no_article", "empty_article", "conflicting_url", "duplicate_url", "external_host",
        "userinfo", "query", "fragment", "port", "encoded_traversal", "traversal",
        "unknown_category", "oversized_html"])
def test_fail_closed_parse(data, code):
    with pytest.raises(OoptError) as exc:
        parse_catalog(data)
    assert exc.value.code == code


def test_cache_provenance_idempotency_and_area(tmp_path):
    data = page()
    store, cache = SnapshotStore(tmp_path / "store"), tmp_path / "cache"
    session = Session(Response(data))
    adapter = LenoblOoptAdapter(store, cache, session=session)
    first = adapter.collect(AREA)
    assert not first.from_cache and session.calls == 1
    assert first.catalog["raw_snapshot_id"] == first.raw_snapshot.snapshot_id
    assert first.catalog["area_id"] == AREA.area_id
    assert first.catalog["query_jurisdiction_applicability"] == "source_reported"
    assert store.read_content(first.raw_snapshot.snapshot_id) == data
    assert json.loads(store.read_content(first.catalog_snapshot.snapshot_id)) == first.catalog
    assert first.raw_snapshot.area_id == first.catalog_snapshot.area_id == AREA.area_id
    assert first.raw_snapshot.completeness == first.catalog_snapshot.completeness == "partial"
    offline = LenoblOoptAdapter(store, cache, offline=True).collect(AREA)
    assert offline.from_cache and offline.raw_snapshot == first.raw_snapshot
    assert offline.catalog_snapshot == first.catalog_snapshot
    assert adapter.collect(AREA).from_cache and session.calls == 1
    other = StudyArea("Другое", AREA.request_geometry)
    assert adapter.collect(other).raw_snapshot.snapshot_id != first.raw_snapshot.snapshot_id
    assert adapter.collect(other).catalog["query_jurisdiction_applicability"] == "unknown"
    assert session.calls == 2


def test_offline_cache_missing_expired_corrupt_and_refresh(tmp_path):
    store, cache = SnapshotStore(tmp_path / "store"), tmp_path / "cache"
    offline = LenoblOoptAdapter(store, cache, offline=True)
    with pytest.raises(OoptError, match="cache_missing"):
        offline.collect(AREA)
    adapter = LenoblOoptAdapter(store, cache, session=Session(Response(page())))
    first = adapter.collect(AREA)
    pointer = next(cache.glob("*.json"))
    record = json.loads(pointer.read_text(encoding="utf-8"))
    checked = datetime.now(timezone.utc) - timedelta(days=2)
    record.update(checked_at=checked.isoformat(), expires_at=(checked + timedelta(days=1)).isoformat())
    pointer.write_text(json.dumps(record))
    with pytest.raises(OoptError, match="cache_expired"):
        offline.collect(AREA)
    refreshed = adapter.collect(AREA)
    assert refreshed.raw_snapshot == first.raw_snapshot
    assert refreshed.catalog_snapshot == first.catalog_snapshot
    assert refreshed.raw_snapshot.retrieved_at == first.raw_snapshot.retrieved_at
    assert refreshed.checked_at > first.checked_at
    record = json.loads(pointer.read_text(encoding="utf-8"))
    record["raw_snapshot_id"] = "missing"
    pointer.write_text(json.dumps(record))
    with pytest.raises(OoptError, match="cache_corrupt"):
        offline.collect(AREA)
    adapter.collect(AREA)
    (tmp_path / "store" / "blobs" / (first.raw_snapshot.content_hash + ".bin")).write_bytes(b"tamper")
    with pytest.raises(OoptError, match="cache_corrupt"):
        offline.collect(AREA)


@pytest.mark.parametrize("response,code", [
    (Response(page(), status=403), "http_403"),
    (Response(page(), status=429), "http_429"),
    (Response(b"captcha"), "challenge_response"),
    (Response(page(), headers={"Content-Length": "1"}), "truncated_response"),
    (Response(page(), headers={"Content-Length": str(MAX_HTML_BYTES + 1)}), "oversized_response"),
    (Response(page(), url="https://elsewhere.test/"), "unsafe_redirect"),
    (requests.exceptions.SSLError("tls"), "tls_error"),
    (requests.exceptions.Timeout("timeout"), "timeout"),
])
def test_negative_http(tmp_path, response, code):
    adapter = LenoblOoptAdapter(SnapshotStore(tmp_path / "store"), tmp_path / "cache",
                               session=Session(response))
    with pytest.raises(OoptError) as exc:
        adapter.collect(AREA)
    assert exc.value.code == code
