"""Official Leningrad Oblast OOPT card index; names are not spatial evidence."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import unquote, urljoin, urlsplit

import requests

from src.data_models.study_area import StudyArea
from src.etl.http_session import BoundedSession
from src.storage import SnapshotStore


SOURCE_URL = ("https://kpr.lenobl.ru/ru/deiatelnost/organizaciya-i-funkcionirovanie-"
              "osobo-ohranyaemyh-prirodnyh-territorij/perechen-osobo-ohranyaemyh-"
              "prirodnyh-territorij-oopt-regionalnogo-i-20/")
SOURCE_ID = "lenobl_oopt_catalog"
PARSER_VERSION = "lenobl-oopt-catalog-v1"
MAX_HTML_BYTES = 4 * 1024 * 1024
MAX_ITEMS = 500
TTL_SECONDS = 86400
USER_AGENT = "LeningradTourismFramework/0.1 (+https://github.com/KTL-deep/leningrad_tourism_model)"
_TYPES = ("Государственный природный комплексный заказник", "Государственный природный заказник",
          "Памятник природы", "Природный парк", "Охраняемый природный ландшафт")


class OoptError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _clean(text):
    return " ".join(text.split()).strip(" \u200b")


def _child_url(href):
    if not href or len(href) > 2048 or any(c.isspace() or ord(c) < 32 for c in href):
        raise OoptError("unsafe_child_url")
    try:
        parts = urlsplit(href)
        url = urljoin(SOURCE_URL, href)
        target = urlsplit(url)
        path = target.path
        decoded = unquote(path)
        if (parts.scheme and parts.scheme != "https" or parts.netloc and parts.hostname != "kpr.lenobl.ru"
                or target.scheme != "https" or target.hostname != "kpr.lenobl.ru"
                or target.username is not None or target.password is not None or target.port is not None
                or target.query or target.fragment or not path.startswith(urlsplit(SOURCE_URL).path)
                or not path.endswith("/") or path == urlsplit(SOURCE_URL).path
                or "%" in path or "\\" in decoded or any(p in (".", "..") for p in decoded.split("/"))
                or not decoded.startswith(urlsplit(SOURCE_URL).path)):
            raise OoptError("unsafe_child_url")
    except (ValueError, TypeError) as exc:
        raise OoptError("unsafe_child_url") from exc
    return url


class _CatalogParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.article = 0
        self.articles = 0
        self.depth = 0
        self.p = False
        self.p_text = []
        self.pending = None
        self.level = None
        self.ul_depth = None
        self.li_depth = None
        self.href = None
        self.link_text = []
        self.items = []
        self.update_depth = None
        self.update_text = []
        self.updates = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag not in ("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"):
            self.depth += 1
        if tag == "article" and attrs.get("data-mistake") == "content":
            self.articles += 1
            if self.articles != 1:
                raise OoptError("ambiguous_article")
            self.article = self.depth
        if tag == "div" and "edit-redactor" in attrs.get("class", "").split():
            self.update_depth = self.depth
            self.update_text = []
        if not self.article:
            return
        if tag == "p":
            self.p, self.p_text = True, []
        elif tag == "ul":
            if self.pending is not None and self.ul_depth is None:
                self.level, self.pending, self.ul_depth = self.pending, None, self.depth
            else:
                self.level = None
        elif tag == "li" and self.ul_depth == self.depth - 1:
            self.li_depth = self.depth
            self.href = None
        elif tag == "a" and self.li_depth is not None:
            if self.href is not None:
                raise OoptError("ambiguous_links")
            self.href = attrs.get("href", "")
            self.link_text = []

    def handle_data(self, data):
        if self.update_depth is not None:
            self.update_text.append(data)
        if self.article and self.p:
            self.p_text.append(data)
        if self.article and self.href is not None:
            self.link_text.append(data)

    def handle_endtag(self, tag):
        if tag in ("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"):
            return
        if self.article:
            if tag == "p" and self.p:
                text = _clean("".join(self.p_text))
                if re.search(r"Кадастровые сведения об ООПТ регионального значения", text, re.I):
                    self.pending = "regional"
                elif re.search(r"Кадастровые сведения об ООПТ (?:местного|метного) значения", text, re.I):
                    self.pending = "local"
                self.p = False
            elif tag == "li" and self.li_depth == self.depth:
                if self.href is None:
                    raise OoptError("ambiguous_links")
                title = _clean("".join(self.link_text))
                category = next((t for t in _TYPES if title.startswith(t + " ") or title.startswith(t + "«")), None)
                if not title or category is None or (self.level == "local") != (category == _TYPES[-1]):
                    raise OoptError("ambiguous_name_or_type")
                url = _child_url(self.href)
                name = title[len(category):].strip().strip("«» ")
                if not name:
                    raise OoptError("ambiguous_name_or_type")
                self.items.append({"source_id": "lenobl_oopt_" + hashlib.sha256(url.encode()).hexdigest(),
                                   "title": title, "name": name, "type": category, "level": self.level,
                                   "source_url": url, "geometry": None, "location_status": "unlocated",
                                   "legal_effective_status": "unknown"})
                if len(self.items) > MAX_ITEMS:
                    raise OoptError("too_many_items")
                self.li_depth = None
                self.href = None
            elif tag == "ul" and self.ul_depth == self.depth:
                self.ul_depth = None
                self.level = None
            elif tag == "article" and self.article == self.depth:
                self.article = 0
        if tag == "div" and self.update_depth == self.depth:
            self.updates.append(_clean("".join(self.update_text)))
            self.update_depth = None
        self.depth -= 1


def parse_catalog(content: bytes) -> dict:
    if not isinstance(content, bytes) or not content or len(content) > MAX_HTML_BYTES:
        raise OoptError("invalid_html_size")
    try:
        parser = _CatalogParser()
        parser.feed(content.decode("utf-8-sig"))
        parser.close()
    except UnicodeError as exc:
        raise OoptError("invalid_html_encoding") from exc
    if (parser.article or parser.articles != 1 or parser.ul_depth is not None or not parser.items
            or {item["level"] for item in parser.items} != {"regional", "local"}):
        raise OoptError("invalid_article")
    urls = {}
    for item in parser.items:
        previous = urls.setdefault(item["source_url"], item)
        if previous is not item:
            raise OoptError("duplicate_child_url" if previous == item else "conflicting_child_url")
    dates = [re.fullmatch(r"Обновлено\s*(\d{2})\.(\d{2})\.(\d{4})", update)
             for update in parser.updates]
    if len(dates) == 1 and dates[0]:
        try:
            reported = datetime.strptime(".".join(dates[0].groups()), "%d.%m.%Y").date().isoformat()
        except ValueError:
            reported = None
    else:
        reported = None
    for item in parser.items:
        item["source_reported_update"] = reported
    return {"schema_version": 1, "parser_version": PARSER_VERSION, "source_id": SOURCE_ID,
            "source_url": SOURCE_URL, "source_sha256": hashlib.sha256(content).hexdigest(),
            "source_reported_update": reported, "source_coverage": "leningrad_oblast_catalog_not_spatially_filtered",
            "completeness": "partial", "redistribution_license": "unknown", "items": parser.items}


def _publish(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".pending-", delete=False) as file:
            pending = Path(file.name)
            json.dump(record, file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(pending, path)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


@dataclass(frozen=True)
class OoptResult:
    catalog: dict
    raw_snapshot: object
    catalog_snapshot: object
    from_cache: bool
    checked_at: datetime


class LenoblOoptAdapter:
    def __init__(self, store: SnapshotStore, cache_dir: Path, session=None, offline=False):
        if type(offline) is not bool:
            raise TypeError("offline must be bool")
        self.store = store
        self.cache_dir = Path(cache_dir)
        self.session = session if session is not None else BoundedSession(cache_dir=self.cache_dir / "http", offline=offline)
        self.offline = offline

    def collect(self, area: StudyArea) -> OoptResult:
        if not isinstance(area, StudyArea):
            raise TypeError("area must be StudyArea")
        path = self.cache_dir / (hashlib.sha256(json.dumps([SOURCE_URL, area.area_id, PARSER_VERSION]).encode()).hexdigest() + ".json")
        now = datetime.now(timezone.utc)
        if path.exists():
            try:
                pointer = json.loads(path.read_text(encoding="utf-8"))
                if set(pointer) != {"raw_snapshot_id", "catalog_snapshot_id", "checked_at", "expires_at"}:
                    raise ValueError("pointer keys")
                checked = datetime.fromisoformat(pointer["checked_at"])
                expiry = datetime.fromisoformat(pointer["expires_at"])
                if (checked.tzinfo is None or expiry.tzinfo is None or checked > now
                        or expiry != checked + timedelta(seconds=TTL_SECONDS)):
                    raise ValueError("pointer time")
                if expiry <= now:
                    raise OoptError("cache_expired")
                raw = self.store.get_snapshot(pointer["raw_snapshot_id"])
                derived = self.store.get_snapshot(pointer["catalog_snapshot_id"])
                if (raw.area_id != area.area_id or raw.url != SOURCE_URL or raw.source_id != SOURCE_ID
                        or raw.parser_version != PARSER_VERSION or raw.media_type != "text/html"
                        or derived.area_id != area.area_id or derived.source_id != SOURCE_ID
                        or derived.url != SOURCE_URL or derived.parser_version != PARSER_VERSION
                        or derived.media_type != "application/json" or derived.completeness != "partial"):
                    raise ValueError("snapshot identity")
                content = self.store.read_content(raw.snapshot_id)
                if len(content) > MAX_HTML_BYTES:
                    raise ValueError("oversized cache")
                catalog = parse_catalog(content)
                catalog.update(raw_snapshot_id=raw.snapshot_id, area_id=area.area_id,
                               query_jurisdiction_applicability="source_reported" if "Ленинградская область" in area.jurisdictions else "unknown")
                encoded = (json.dumps(catalog, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
                if self.store.read_content(derived.snapshot_id) != encoded:
                    raise ValueError("derived mismatch")
                return OoptResult(catalog, raw, derived, True, checked)
            except OoptError:
                if self.offline:
                    raise
            except (OSError, ValueError, TypeError, KeyError):
                if self.offline:
                    raise OoptError("cache_corrupt") from None
        elif self.offline:
            raise OoptError("cache_missing")

        try:
            with self.session.get(SOURCE_URL, timeout=(10, 30), stream=True, verify=True,
                                  allow_redirects=False, headers={"User-Agent": USER_AGENT}) as response:
                status = response.status_code
                if status != 200:
                    raise OoptError("http_" + str(status))
                if response.url != SOURCE_URL:
                    raise OoptError("unsafe_redirect")
                if response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "text/html":
                    raise OoptError("invalid_media_type")
                length = response.headers.get("Content-Length")
                if length is not None and (not length.isdecimal() or int(length) > MAX_HTML_BYTES):
                    raise OoptError("oversized_response")
                body = bytearray()
                for chunk in response.iter_content(chunk_size=65536):
                    body.extend(chunk)
                    if len(body) > MAX_HTML_BYTES:
                        raise OoptError("oversized_response")
                if length is not None and len(body) != int(length):
                    raise OoptError("truncated_response")
                content = bytes(body)
        except requests.exceptions.SSLError as exc:
            raise OoptError("tls_error") from exc
        except requests.exceptions.Timeout as exc:
            raise OoptError("timeout") from exc
        except requests.RequestException as exc:
            raise OoptError("network_error") from exc
        if re.search(rb"(?i)captcha|cloudflare|access denied", content):
            raise OoptError("challenge_response")
        catalog = parse_catalog(content)
        raw = self.store.save_snapshot(SOURCE_ID, area.area_id, SOURCE_URL, content, PARSER_VERSION, "text/html", "partial")
        catalog.update(raw_snapshot_id=raw.snapshot_id, area_id=area.area_id,
                       query_jurisdiction_applicability="source_reported" if "Ленинградская область" in area.jurisdictions else "unknown")
        encoded = (json.dumps(catalog, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        derived = self.store.save_snapshot(SOURCE_ID, area.area_id, SOURCE_URL, encoded, PARSER_VERSION,
                                           "application/json", "partial")
        checked = datetime.now(timezone.utc)
        _publish(path, {"raw_snapshot_id": raw.snapshot_id, "catalog_snapshot_id": derived.snapshot_id,
                        "checked_at": checked.isoformat(),
                        "expires_at": (checked + timedelta(seconds=TTL_SECONDS)).isoformat()})
        return OoptResult(catalog, raw, derived, False, checked)
