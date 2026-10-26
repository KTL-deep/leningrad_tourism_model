"""Bounded official-index discovery, without any verified live dataset schema.

The municipal-statistics-v1 shape below is a synthetic probe contract only.
Matching it does not attest a real upstream schema or authorize inclusion.
"""

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urljoin, urlsplit

import requests

from src.data_models.study_area import StudyArea
from src.storage import SnapshotStore


SOURCE_ID = "rosstat_petrostat"
SEED = "https://78.rosstat.gov.ru/"
PARSER_VERSION = "official-statistics-discovery-v1"
HOSTS = frozenset({"78.rosstat.gov.ru"})
MAX_BYTES = 1_000_000
MAX_INDEX = 3
MAX_DATASETS = 2
TTL_SECONDS = 86400


def _publish_pointer(path, record):
    """Replace a cache pointer only after a complete, synced temporary write."""
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


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.extend(value for key, value in attrs if key == "href" and value)


def _url(url):
    try:
        parts = urlsplit(url)
        return (parts.scheme == "https" and parts.hostname in HOSTS and
                parts.username is None and parts.password is None and
                parts.port is None and not parts.fragment and not parts.query and
                len(url) <= 2048)
    except (TypeError, ValueError):
        return False


def _catalog_link(base, href):
    url = urljoin(base, href)
    path = urlsplit(url).path.lower()
    return url if _url(url) and any(part in path for part in ("opendata", "open-data", "catalog")) else None


@dataclass(frozen=True)
class DiscoveryResult:
    status: str
    reason: str | None
    statistics: tuple[dict, ...]
    snapshot_ids: tuple[str, ...]
    requests: tuple[dict, ...]
    parser_version: str = PARSER_VERSION
    candidate: None = None


class _Excluded(Exception):
    def __init__(self, reason):
        self.reason = reason


class OfficialStatisticsAdapter:
    def __init__(self, store: SnapshotStore, cache_dir: Path, session=None, offline=False):
        if type(offline) is not bool:
            raise TypeError("offline must be bool")
        self.store = store
        self.cache_dir = Path(cache_dir)
        self.session = session if session is not None else requests.Session()
        self.offline = offline

    def _fetch(self, url, area, log, snapshots):
        key = hashlib.sha256(json.dumps([url, area.area_id, PARSER_VERSION]).encode()).hexdigest()
        path = self.cache_dir / (key + ".json")
        if path.exists():
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(record, dict) or set(record) != {"snapshot_id", "checked_at", "expires_at", "media_type"}:
                    raise ValueError("invalid pointer")
                checked = datetime.fromisoformat(record["checked_at"])
                expiry = datetime.fromisoformat(record["expires_at"])
                now = datetime.now(timezone.utc)
                if (checked.tzinfo is None or expiry.tzinfo is None or checked > now or
                        expiry <= checked or expiry > checked + timedelta(seconds=TTL_SECONDS)):
                    raise ValueError("invalid cache time window")
                if expiry <= now:
                    raise _Excluded("cache_expired")
                snapshot = self.store.get_snapshot(record["snapshot_id"])
                if (snapshot.url != url or snapshot.area_id != area.area_id or
                        snapshot.source_id != SOURCE_ID or snapshot.parser_version != PARSER_VERSION or
                        snapshot.media_type != record["media_type"]):
                    raise ValueError("cache identity mismatch")
                data = self.store.read_content(snapshot.snapshot_id)
                if len(data) > MAX_BYTES:
                    raise ValueError("oversized cached response")
                media = record["media_type"]
            except _Excluded as exc:
                log.append({"url": url, "outcome": exc.reason})
                if self.offline:
                    raise
            except (OSError, ValueError, TypeError, KeyError):
                log.append({"url": url, "outcome": "cache_corrupt"})
                if self.offline:
                    raise _Excluded("cache_corrupt") from None
            else:
                log.append({"url": url, "outcome": "cache_hit", "snapshot_id": snapshot.snapshot_id})
                snapshots.append(snapshot.snapshot_id)
                return data, media, snapshot.snapshot_id
        elif self.offline:
            log.append({"url": url, "outcome": "cache_missing"})
            raise _Excluded("cache_missing")
        if not self.offline:
            try:
                with self.session.get(url, timeout=(10, 30), stream=True, verify=True,
                                      allow_redirects=False) as response:
                    code = response.status_code
                    if code != 200:
                        raise _Excluded(f"http_{code}")
                    if response.url != url or not _url(response.url):
                        raise _Excluded("unsafe_redirect")
                    media = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                    if media not in ("text/html", "application/json", "text/csv", "application/csv"):
                        raise _Excluded("unsupported_schema")
                    length = response.headers.get("Content-Length")
                    if length is not None and (not length.isdecimal() or int(length) > MAX_BYTES):
                        raise _Excluded("oversized_response")
                    body = bytearray()
                    for chunk in response.iter_content(chunk_size=65536):
                        body.extend(chunk)
                        if len(body) > MAX_BYTES:
                            raise _Excluded("oversized_response")
                    if length is not None and len(body) != int(length):
                        raise _Excluded("truncated_response")
                    data = bytes(body)
            except requests.exceptions.SSLError:
                log.append({"url": url, "outcome": "tls_error"})
                raise _Excluded("tls_error") from None
            except requests.exceptions.Timeout:
                log.append({"url": url, "outcome": "timeout"})
                raise _Excluded("timeout") from None
            except requests.RequestException:
                log.append({"url": url, "outcome": "network_error"})
                raise _Excluded("network_error") from None
            except _Excluded as exc:
                if not log or log[-1] != {"url": url, "outcome": exc.reason}:
                    log.append({"url": url, "outcome": exc.reason})
                raise
            snapshot = self.store.save_snapshot(SOURCE_ID, area.area_id, url, data,
                                                PARSER_VERSION, media)
            checked = datetime.now(timezone.utc)
            _publish_pointer(path, {"snapshot_id": snapshot.snapshot_id, "media_type": media,
                                    "checked_at": checked.isoformat(),
                                    "expires_at": (checked + timedelta(seconds=TTL_SECONDS)).isoformat()})
            log.append({"url": url, "outcome": "ok", "snapshot_id": snapshot.snapshot_id})
        snapshots.append(snapshot.snapshot_id)
        return data, media, snapshot.snapshot_id

    def discover(self, area: StudyArea) -> DiscoveryResult:
        if not isinstance(area, StudyArea):
            raise TypeError("area must be StudyArea")
        log, snapshots = [], []

        def excluded(reason):
            return DiscoveryResult("excluded", reason, (), tuple(snapshots), tuple(log))

        try:
            queue, seen, catalogs = [SEED], set(), []
            invalid_catalog = False
            while queue and len(seen) < MAX_INDEX:
                url = queue.pop(0)
                if url in seen:
                    continue
                seen.add(url)
                data, media, _index_sid = self._fetch(url, area, log, snapshots)
                if media == "application/json":
                    try:
                        doc = json.loads(data.decode("utf-8"))
                    except (ValueError, UnicodeError):
                        raise _Excluded("unsupported_schema") from None
                    if isinstance(doc, dict) and isinstance(doc.get("datasets"), list):
                        catalogs.append(doc)
                    else:
                        invalid_catalog = True
                    continue
                if media != "text/html":
                    continue
                links = _Links()
                try:
                    links.feed(data.decode("utf-8"))
                except UnicodeError:
                    raise _Excluded("unsupported_schema") from None
                for href in links.links[:200]:
                    link = _catalog_link(url, href)
                    if link and link not in seen and link not in queue:
                        queue.append(link)
            if not catalogs:
                return excluded("unsupported_schema" if invalid_catalog else "not_found")
            reasons = []
            attempted = 0
            for catalog in catalogs:
                if catalog.get("publisher") not in ("Петростат", "Росстат"):
                    reasons.append("unsupported_schema")
                    continue
                if catalog.get("license") not in ("CC BY 4.0", "CC0 1.0"):
                    reasons.append("unknown_license")
                    continue
                for item in catalog["datasets"][:200]:
                    if not isinstance(item, dict) or item.get("schema") != "municipal-statistics-v1":
                        reasons.append("unsupported_schema")
                        continue
                    if (item.get("municipality") != area.name or
                            not area.jurisdictions or item.get("jurisdiction") not in area.jurisdictions):
                        reasons.append("coverage_mismatch")
                        continue
                    url = item.get("url")
                    if not _url(url) or not urlsplit(url).path.lower().endswith(".csv"):
                        reasons.append("unsupported_schema")
                        continue
                    if attempted >= MAX_DATASETS:
                        break
                    attempted += 1
                    data, media, _table_sid = self._fetch(url, area, log, snapshots)
                    if media not in ("text/csv", "application/csv"):
                        reasons.append("unsupported_schema")
                        continue
                    try:
                        reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline=""))
                        if reader.fieldnames != ["municipality", "jurisdiction", "period", "metric", "unit", "value"]:
                            raise ValueError("columns")
                        rows = list(reader)
                        if not rows or len(rows) > 10000:
                            raise ValueError("row count")
                        for row in rows:
                            if set(row) != set(reader.fieldnames) or any(value is None for value in row.values()):
                                raise ValueError("row shape")
                            if row["municipality"] != area.name or row["jurisdiction"] != item["jurisdiction"]:
                                raise _Excluded("coverage_mismatch")
                            if (not row["period"].isdigit() or len(row["period"]) != 4 or
                                    not 1900 <= int(row["period"]) <= 2100 or
                                    not row["value"].isdigit() or not row["metric"].strip() or
                                    not row["unit"].strip() or
                                    row["period"] != str(item.get("period")) or
                                    row["metric"] != item.get("metric") or row["unit"] != item.get("unit")):
                                raise ValueError("row contract")
                    except _Excluded:
                        raise
                    except (UnicodeError, ValueError, TypeError, KeyError, csv.Error):
                        reasons.append("unsupported_schema")
                        continue
                    # A fixture-compatible shape cannot establish a live Rosstat schema.
                    # Retain both immutable catalog and table snapshots, not statistics.
                    return excluded("unverified_live_schema")
            return excluded(reasons[0] if reasons else "not_found")
        except _Excluded as exc:
            return excluded(exc.reason)


def discover(area: StudyArea, *, store: SnapshotStore, cache_dir: Path, session=None,
             offline=False) -> DiscoveryResult:
    return OfficialStatisticsAdapter(store, cache_dir, session=session, offline=offline).discover(area)
