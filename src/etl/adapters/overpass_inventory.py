"""Bounded Overpass inventory collection with verified raw-snapshot caching."""

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

from pyproj import Geod
import requests
from shapely.geometry import shape

from src.data_models.study_area import StudyArea
from src.storage import SnapshotStore


ENDPOINT = "https://overpass.private.coffee/api/interpreter"
SOURCE_ID = "osm_overpass_inventory"
ADAPTER_VERSION = "overpass-inventory-v1"
USER_AGENT = "LeningradTourismFramework/0.1 (+https://github.com/KTL-deep/leningrad_tourism_model)"
ATTRIBUTION = "© OpenStreetMap contributors (ODbL)"
MAX_BYTES = 16 * 1024 * 1024
INTERVAL = 1.1
MAX_BBOX_KM2 = 1000


class OverpassError(Exception):
    def __init__(self, code: str, details=None):
        self.code = code
        self.details = details
        super().__init__(code)


@dataclass(frozen=True)
class InventoryCollection:
    snapshot: object
    inventory: object
    query_bbox: tuple[float, float, float, float]
    from_cache: bool
    observed_at: datetime
    attribution: str = ATTRIBUTION


def _now():
    return datetime.now(timezone.utc)


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be text")
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


def collection_bbox(study_area: StudyArea) -> tuple[float, float, float, float]:
    """Return the explicit query rectangle, in Overpass south/west/north/east order."""
    if not isinstance(study_area, StudyArea):
        raise OverpassError("invalid_study_area")
    geometry = study_area.analysis_geometry or study_area.request_geometry
    west, south, east, north = shape(geometry).bounds
    if east - west >= 180 or north <= south or east <= west:
        raise OverpassError("bbox_too_large")
    area, _ = Geod(ellps="WGS84").polygon_area_perimeter(
        [west, east, east, west], [south, south, north, north])
    if not math.isfinite(area) or abs(area) / 1_000_000 > MAX_BBOX_KM2:
        raise OverpassError("bbox_too_large", details={"max_km2": MAX_BBOX_KM2})
    return south, west, north, east


def build_query(study_area: StudyArea) -> str:
    # Round-trip-safe floats: rounding a bound inward can lose border objects.
    bbox = ",".join(repr(value) for value in collection_bbox(study_area))
    filters = (
        '["tourism"]', '["historic"]',
        '["amenity"~"^(cafe|restaurant|fast_food|bar|pub|food_court|toilets|drinking_water|parking|hospital|clinic|pharmacy|bus_station|ferry_terminal)$"]',
        '["leisure"~"^(park|garden|nature_reserve)$"]',
        '["shop"~"^(supermarket|convenience)$"]',
        '["railway"~"^(station|halt)$"]', '["highway"="bus_stop"]',
        '["public_transport"~"^(station|platform)$"]',
    )
    lines = [f"  nwr{tag}({bbox});" for tag in filters]
    lines.append(f'  way["highway"]({bbox});')
    return ("[out:json][timeout:25][maxsize:134217728];\n(\n" + "\n".join(lines)
            + "\n);\n(._;>;);\nout body center;\nout count;")


def _parse(content, study_area):
    # The inventory parser validates Overpass count, timestamp, remark and schema.
    from src.etl.osm_parsing import parse_overpass
    try:
        inventory = parse_overpass(content, study_area)
        inventory.to_dict()
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise OverpassError("invalid_response", details={"validation_error": getattr(exc, "code", type(exc).__name__)}) from exc
    return inventory


class OverpassInventoryAdapter:
    def __init__(self, store: SnapshotStore, cache_dir: Path, session=None,
                 ttl_seconds=86400, offline=False, endpoint=ENDPOINT,
                 terms_confirmed=False, max_attempts=2):
        if (type(ttl_seconds) not in (int, float) or not math.isfinite(ttl_seconds)
                or ttl_seconds <= 0):
            raise ValueError("ttl_seconds must be positive")
        if type(offline) is not bool or type(terms_confirmed) is not bool:
            raise ValueError("offline and terms_confirmed must be bool")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 2:
            raise ValueError("max_attempts must be 1 or 2")
        try:
            parsed = urlsplit(endpoint)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or parsed.query or parsed.fragment
                    or parsed.path != "/api/interpreter" or parsed.port == 0):
                raise ValueError("invalid endpoint")
        except (TypeError, ValueError) as exc:
            raise ValueError("endpoint must be an HTTPS /api/interpreter URL without credentials or parameters") from exc
        self.store = store
        self.cache_dir = Path(cache_dir)
        self.session = session if session is not None else requests.Session()
        self.ttl_seconds = ttl_seconds
        self.offline = offline
        self.endpoint = endpoint
        self.terms_confirmed = terms_confirmed
        self.max_attempts = max_attempts

    def _rate_limit(self):
        last_path = self.cache_dir / "last_request.json"
        if last_path.exists():
            try:
                last = json.loads(last_path.read_text(encoding="utf-8"))
                if not isinstance(last, dict) or set(last) != {"at"}:
                    raise ValueError("invalid rate state")
                elapsed = (_now() - _timestamp(last["at"])).total_seconds()
                if elapsed < 0:
                    raise ValueError("future rate state")
            except (ValueError, TypeError, KeyError, OSError) as exc:
                raise OverpassError("invalid_rate_state") from exc
            if elapsed < INTERVAL:
                time.sleep(INTERVAL - elapsed)
        observed = _now()
        _atomic_json(last_path, {"at": observed.isoformat()})
        return observed

    def _fetch(self, query):
        try:
            with self.session.post(self.endpoint, data={"data": query},
                                   headers={"User-Agent": USER_AGENT}, timeout=(10, 45),
                                   verify=True, stream=True, allow_redirects=False) as response:
                status = response.status_code
                if status in (403, 429):
                    raise OverpassError("access_denied" if status == 403 else "rate_limited",
                                        details={"http_status": status})
                if status != 200:
                    raise OverpassError("http_error", details={"http_status": status})
                media_type = response.headers.get("Content-Type")
                if media_type is not None and media_type.split(";", 1)[0].strip().lower() != "application/json":
                    raise OverpassError("invalid_response")
                try:
                    length = int(response.headers.get("Content-Length", "0"))
                    if length < 0:
                        raise ValueError("negative length")
                except (ValueError, TypeError) as exc:
                    raise OverpassError("invalid_response") from exc
                if length > MAX_BYTES:
                    raise OverpassError("response_too_large")
                data = bytearray()
                for chunk in response.iter_content(chunk_size=65536):
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        raise OverpassError("response_too_large")
                return bytes(data)
        except requests.exceptions.SSLError as exc:
            raise OverpassError("tls_error") from exc
        except requests.exceptions.Timeout as exc:
            raise OverpassError("timeout") from exc
        except requests.RequestException as exc:
            raise OverpassError("fetch_failed", details={"error_class": type(exc).__name__}) from exc

    def collect(self, study_area: StudyArea) -> InventoryCollection:
        if self.endpoint != ENDPOINT and not self.terms_confirmed:
            raise OverpassError("access_not_permitted")
        bbox = collection_bbox(study_area)
        query = build_query(study_area)
        url = self.endpoint + "#query=" + hashlib.sha256(query.encode("utf-8")).hexdigest()
        key = hashlib.sha256(json.dumps([self.endpoint, query, study_area.area_id, ADAPTER_VERSION],
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
                    raise ValueError("invalid pointer")
                checked = _timestamp(previous["checked_at"])
                expires = _timestamp(previous["expires_at"])
                if expires <= checked or checked > now:
                    raise ValueError("invalid expiry")
                expires = min(expires, checked + timedelta(seconds=self.ttl_seconds))
                snapshot = self.store.get_snapshot(previous["snapshot_id"])
                if (snapshot.source_id != SOURCE_ID or snapshot.parser_version != ADAPTER_VERSION
                        or snapshot.area_id != study_area.area_id or snapshot.url != url
                        or snapshot.completeness != "complete"):
                    raise ValueError("cache provenance mismatch")
                raw = self.store.read_content(snapshot.snapshot_id)
                if len(raw) > MAX_BYTES:
                    raise ValueError("oversized cache")
                inventory = _parse(raw, study_area)
            except (ValueError, TypeError, KeyError, OSError, OverpassError) as exc:
                raise OverpassError("invalid_cache") from exc
            if expires > now:
                return InventoryCollection(snapshot, inventory, bbox, True, checked)
        if self.offline:
            raise OverpassError("cache_expired" if previous else "cache_miss")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        lock = self.cache_dir / "request.lock"
        token = uuid.uuid4().hex
        try:
            with lock.open("x", encoding="utf-8") as file:
                file.write(token)
        except FileExistsError as exc:
            raise OverpassError("request_locked") from exc
        try:
            for attempt in range(self.max_attempts):
                observed = self._rate_limit()
                try:
                    raw = self._fetch(query)
                    break
                except OverpassError as exc:
                    if attempt + 1 == self.max_attempts or not (exc.code == "timeout" or
                            exc.code == "http_error" and exc.details["http_status"] in (502, 503, 504)):
                        raise
                    time.sleep(2)
            inventory = _parse(raw, study_area)
            snapshot = self.store.save_snapshot(SOURCE_ID, study_area.area_id, url, raw,
                                                ADAPTER_VERSION, "application/json", "complete",
                                                retrieved_at=observed)
            _atomic_json(path, {"schema_version": 1, "key": key,
                                "snapshot_id": snapshot.snapshot_id,
                                "checked_at": observed.isoformat(),
                                "expires_at": (observed + timedelta(seconds=self.ttl_seconds)).isoformat()})
            return InventoryCollection(snapshot, inventory, bbox, False, observed)
        finally:
            if lock.read_text(encoding="utf-8") == token:
                lock.unlink()
