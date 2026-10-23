"""Explicit, single-territory Nominatim polygon lookup with verified offline cache."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time
import uuid
from urllib.parse import urlsplit

import requests
from shapely.errors import GEOSException

from src.data_models.study_area import StudyArea
from src.storage import SnapshotStore


ENDPOINT = "https://nominatim.openstreetmap.org/search"
SOURCE_ID = "osm_nominatim"
ADAPTER_VERSION = "nominatim-boundary-v1"
USER_AGENT = "LeningradTourismFramework/0.1 (+https://github.com/KTL-deep/leningrad_tourism_model)"
ATTRIBUTION = "© OpenStreetMap contributors (ODbL)"
MAX_BYTES = 2 * 1024 * 1024
INTERVAL = 1.1
_TYPES = frozenset({"administrative", "city", "town", "village", "municipality", "suburb"})


class BoundaryError(Exception):
    def __init__(self, code: str, details=None, *, candidates=None):
        self.code = code
        self.details = details if candidates is None else {"candidates": candidates}
        super().__init__(code)


@dataclass(frozen=True)
class BoundaryCollection:
    study_area: StudyArea
    snapshot: object
    from_cache: bool
    observed_at: datetime
    attribution: str = ATTRIBUTION


def _now():
    return datetime.now(timezone.utc)


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed


def _atomic_json(path, data):
    pending = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".pending-", delete=False) as file:
            pending = Path(file.name)
            json.dump(data, file, ensure_ascii=False, allow_nan=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(pending, path)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def _select(content: bytes, query: str) -> StudyArea:
    try:
        records = json.loads(content.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise BoundaryError("invalid_response") from exc
    if not isinstance(records, list) or len(records) > 5:
        raise BoundaryError("invalid_response")
    selected = {}
    for record in records:
        if not isinstance(record, dict):
            raise BoundaryError("invalid_response")
        category = record.get("category", record.get("class"))
        kind = record.get("type")
        geometry = record.get("geojson")
        if not isinstance(category, str) or not isinstance(kind, str) or not isinstance(geometry, dict):
            raise BoundaryError("invalid_response")
        if category not in ("boundary", "place") or kind not in _TYPES:
            continue
        if geometry.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        osm_type, osm_id = record.get("osm_type"), record.get("osm_id")
        name = record.get("display_name", query)
        address = record.get("address", {})
        if (osm_type not in ("way", "relation") or type(osm_id) is not int or osm_id <= 0
                or not isinstance(name, str) or not name.strip()
                or not isinstance(address, dict)):
            raise BoundaryError("invalid_response")
        state = address.get("state")
        if state is not None and not isinstance(state, str):
            raise BoundaryError("invalid_response")
        try:
            area = StudyArea(name=name, request_geometry=geometry,
                             jurisdictions=(state.strip(),) if state and state.strip() else (),
                             jurisdiction_status="source_reported" if state and state.strip() else "unknown")
        except (ValueError, TypeError, KeyError, IndexError, GEOSException) as exc:
            raise BoundaryError("invalid_geometry") from exc
        identity = (osm_type, osm_id)
        if identity in selected and selected[identity].area_id != area.area_id:
            raise BoundaryError("conflicting_boundary")
        selected.setdefault(identity, area)
    if not selected:
        raise BoundaryError("not_found")
    if len(selected) > 1:
        raise BoundaryError("ambiguous_boundary", candidates=[
            {"osm_type": osm_type, "osm_id": osm_id, "name": area.name}
            for (osm_type, osm_id), area in selected.items()
        ])
    return next(iter(selected.values()))


class NominatimBoundaryAdapter:
    def __init__(self, store: SnapshotStore, cache_dir: Path, session=None,
                 ttl_seconds=86400, offline=False, endpoint=ENDPOINT, terms_confirmed=False):
        if (isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float))
                or not math.isfinite(ttl_seconds) or ttl_seconds <= 0):
            raise ValueError("ttl_seconds must be positive")
        if type(offline) is not bool or type(terms_confirmed) is not bool:
            raise ValueError("offline and terms_confirmed must be bool")
        try:
            parsed = urlsplit(endpoint)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or parsed.query or parsed.fragment
                    or parsed.path != "/search" or parsed.port == 0):
                raise ValueError("endpoint must be an HTTPS /search URL without credentials or parameters")
        except (TypeError, ValueError) as exc:
            raise ValueError("endpoint must be an HTTPS /search URL without credentials or parameters") from exc
        self.store = store
        self.cache_dir = Path(cache_dir)
        self.session = session if session is not None else requests.Session()
        self.ttl_seconds = ttl_seconds
        self.offline = offline
        self.endpoint = endpoint
        self.terms_confirmed = terms_confirmed

    def collect(self, query: str) -> BoundaryCollection:
        if self.endpoint != ENDPOINT and self.terms_confirmed is not True:
            raise BoundaryError("access_not_permitted")
        if not isinstance(query, str) or not query.strip():
            raise BoundaryError("invalid_query")
        query = " ".join(query.split()).casefold()
        params = {"q": query, "format": "jsonv2", "addressdetails": "1",
                  "polygon_geojson": "1", "limit": "5", "countrycodes": "ru",
                  "accept-language": "ru"}
        url = requests.Request("GET", self.endpoint, params=params).prepare().url
        key = hashlib.sha256(json.dumps([self.endpoint, params, ADAPTER_VERSION], sort_keys=True,
                                        ensure_ascii=False).encode("utf-8")).hexdigest()
        path = self.cache_dir / (key + ".json")
        now = _now()
        previous = None
        if path.exists():
            try:
                previous = json.loads(path.read_text(encoding="utf-8"))
                if (not isinstance(previous, dict) or set(previous) !=
                        {"schema_version", "snapshot_id", "checked_at", "expires_at", "key"}
                        or type(previous["schema_version"]) is not int or previous["schema_version"] != 1
                        or previous["key"] != key or not isinstance(previous["snapshot_id"], str)):
                    raise ValueError("invalid cache pointer")
                checked = _timestamp(previous["checked_at"])
                expires = _timestamp(previous["expires_at"])
                if expires <= checked or checked > now:
                    raise ValueError("invalid cache expiry")
                expires = min(expires, checked + timedelta(seconds=self.ttl_seconds))
                snapshot = self.store.get_snapshot(previous["snapshot_id"])
                if (snapshot.source_id != SOURCE_ID or snapshot.parser_version != ADAPTER_VERSION
                        or snapshot.url != url or snapshot.completeness != "complete"):
                    raise ValueError("cache provenance mismatch")
                raw = self.store.read_content(snapshot.snapshot_id)
                if len(raw) > MAX_BYTES:
                    raise ValueError("oversized cached response")
                area = _select(raw, query)
                if area.area_id != snapshot.area_id:
                    raise ValueError("cache area mismatch")
            except (ValueError, TypeError, KeyError, OSError, BoundaryError) as exc:
                raise BoundaryError("invalid_cache") from exc
            if expires > now:
                return BoundaryCollection(area, snapshot, True, checked)
        if self.offline:
            raise BoundaryError("cache_expired" if previous else "cache_miss")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        lock = self.cache_dir / "request.lock"
        token = uuid.uuid4().hex
        try:
            with lock.open("x", encoding="utf-8") as file:
                file.write(token)
        except FileExistsError as exc:
            raise BoundaryError("request_locked") from exc
        try:
            last_path = self.cache_dir / "last_request.json"
            if last_path.exists():
                try:
                    last = json.loads(last_path.read_text(encoding="utf-8"))
                    if not isinstance(last, dict) or set(last) != {"at"}:
                        raise ValueError("invalid last request")
                    elapsed = (_now() - _timestamp(last["at"])).total_seconds()
                    if elapsed < 0:
                        raise ValueError("last request timestamp is in the future")
                    remaining = INTERVAL - elapsed
                except (ValueError, KeyError, TypeError, OSError) as exc:
                    raise BoundaryError("invalid_rate_state") from exc
                if remaining > 0:
                    time.sleep(remaining)
            observed = _now()
            _atomic_json(last_path, {"at": observed.isoformat()})
            try:
                with self.session.get(self.endpoint, params=params, headers={"User-Agent": USER_AGENT},
                                      timeout=(10, 25), verify=True, stream=True,
                                      allow_redirects=False) as response:
                    if response.status_code in (403, 429):
                        raise BoundaryError(
                            "access_denied" if response.status_code == 403 else "rate_limited",
                            details={"http_status": response.status_code},
                        )
                    if response.status_code != 200:
                        raise BoundaryError("http_error", details={"http_status": response.status_code})
                    media_type = response.headers.get("Content-Type")
                    if media_type is not None and media_type.split(";", 1)[0].strip().lower() != "application/json":
                        raise BoundaryError("invalid_response")
                    try:
                        length = int(response.headers.get("Content-Length", "0"))
                        if length < 0:
                            raise ValueError("negative length")
                    except (ValueError, TypeError) as exc:
                        raise BoundaryError("invalid_response") from exc
                    if length > MAX_BYTES:
                        raise BoundaryError("response_too_large")
                    data = bytearray()
                    for chunk in response.iter_content(chunk_size=65536):
                        data.extend(chunk)
                        if len(data) > MAX_BYTES:
                            raise BoundaryError("response_too_large")
            except requests.exceptions.SSLError as exc:
                raise BoundaryError("tls_error") from exc
            except requests.exceptions.Timeout as exc:
                raise BoundaryError("timeout") from exc
            except requests.RequestException as exc:
                raise BoundaryError("fetch_failed", details={"error_class": type(exc).__name__}) from exc
            area = _select(bytes(data), query)
            snapshot = self.store.save_snapshot(SOURCE_ID, area.area_id, url, bytes(data),
                                                ADAPTER_VERSION, "application/json", "complete",
                                                retrieved_at=observed)
            _atomic_json(path, {"schema_version": 1, "key": key,
                                "snapshot_id": snapshot.snapshot_id,
                                "checked_at": observed.isoformat(),
                                "expires_at": (observed + timedelta(seconds=self.ttl_seconds)).isoformat()})
            return BoundaryCollection(area, snapshot, False, observed)
        finally:
            if lock.read_text(encoding="utf-8") == token:
                lock.unlink()
