"""Overpass API adapter for loading OSM POI within a study area boundary."""

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
from shapely.geometry import shape

from src.data_models.study_area import StudyArea
from src.storage import SnapshotStore


ENDPOINT = "https://overpass-api.de/api/interpreter"
SOURCE_ID = "osm_overpass_poi"
ADAPTER_VERSION = "overpass-poi-v1"
USER_AGENT = "LeningradTourismFramework/0.1 (+https://github.com/KTL-deep/leningrad_tourism_model)"
ATTRIBUTION = "© OpenStreetMap contributors (ODbL)"
MAX_BYTES = 50 * 1024 * 1024
INTERVAL = 1.1

POI_TAGS = {
    "amenity": [
        "cafe", "restaurant", "fast_food", "bar", "pub", "food_court",
        "hospital", "clinic", "pharmacy", "toilets", "parking",
    ],
    "tourism": [
        "hotel", "motel", "hostel", "guest_house", "camp_site",
        "museum", "gallery", "attraction", "viewpoint", "information",
    ],
    "historic": None,  # all values
    "leisure": [
        "park", "garden", "nature_reserve", "playground", "sports_centre",
    ],
    "shop": ["supermarket", "convenience"],
}


class POIError(Exception):
    """Typed error raised by the Overpass POI adapter."""

    def __init__(self, code: str, details=None):
        self.code = code
        self.details = details
        super().__init__(code)


@dataclass(frozen=True)
class POICollection:
    """Result of an Overpass POI query for a study area."""

    study_area: StudyArea
    snapshot: object
    poi_count: int
    category_counts: dict
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


def _polygon_filter(study_area: StudyArea) -> str:
    """Build Overpass ``poly:"lat1 lon1 ..."`` filter from the study area exterior ring."""
    geom = shape(study_area.request_geometry)
    if geom.geom_type == "MultiPolygon":
        # Use the largest polygon from the MultiPolygon
        geom = max(geom.geoms, key=lambda g: g.area)
    exterior = geom.exterior
    # Overpass poly filter uses "lat lon" pairs (note: GeoJSON is lon,lat)
    pairs = []
    for lon, lat in exterior.coords:
        pairs.append(f"{lat} {lon}")
    return " ".join(pairs)


def _build_query(study_area: StudyArea) -> str:
    """Build an Overpass QL query for tourism-relevant POI categories."""
    poly = _polygon_filter(study_area)
    filters = []
    for tag_key, tag_values in POI_TAGS.items():
        if tag_values is None:
            # Match any value for this key (e.g. historic=*)
            filters.append(f'node["{tag_key}"](poly:"{poly}");')
            filters.append(f'way["{tag_key}"](poly:"{poly}");')
            filters.append(f'relation["{tag_key}"](poly:"{poly}");')
        else:
            regex = "|".join(tag_values)
            filters.append(f'node["{tag_key}"~"^({regex})$"](poly:"{poly}");')
            filters.append(f'way["{tag_key}"~"^({regex})$"](poly:"{poly}");')
            filters.append(f'relation["{tag_key}"~"^({regex})$"](poly:"{poly}");')
    body = "\n  ".join(filters)
    return f"[out:json][timeout:120];\n(\n  {body}\n);\nout center;"


def _parse_elements(data: bytes) -> tuple[list[dict], dict[str, int]]:
    """Parse Overpass JSON response and return (poi_list, category_counts).

    Each POI dict contains: id, type, lat, lon, tags.
    """
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise POIError("invalid_response") from exc

    if not isinstance(payload, dict):
        raise POIError("invalid_response")

    elements = payload.get("elements")
    if not isinstance(elements, list):
        raise POIError("invalid_response")

    pois: list[dict] = []
    category_counts: dict[str, int] = {}

    for element in elements:
        if not isinstance(element, dict):
            raise POIError("invalid_response")
        etype = element.get("type")
        eid = element.get("id")
        if not isinstance(etype, str) or etype not in ("node", "way", "relation"):
            continue
        if not isinstance(eid, int):
            continue

        tags = element.get("tags")
        if not isinstance(tags, dict):
            continue

        # Resolve coordinates: nodes have lat/lon directly, ways/relations use center
        if etype == "node":
            lat = element.get("lat")
            lon = element.get("lon")
        else:
            center = element.get("center")
            if not isinstance(center, dict):
                continue
            lat = center.get("lat")
            lon = center.get("lon")

        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        if isinstance(lat, bool) or isinstance(lon, bool):
            continue

        poi = {"id": eid, "type": etype, "lat": lat, "lon": lon, "tags": tags}
        pois.append(poi)

        # Count by matched POI category
        for tag_key in POI_TAGS:
            if tag_key in tags:
                value = tags[tag_key]
                category = f"{tag_key}={value}"
                category_counts[category] = category_counts.get(category, 0) + 1

    return pois, category_counts


class OverpassPOIAdapter:
    """Fetch OSM POI data within a study area via the Overpass API."""

    def __init__(self, store: SnapshotStore, cache_dir: Path, session=None,
                 ttl_seconds=86400, offline=False, endpoint=ENDPOINT):
        if (isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float))
                or not math.isfinite(ttl_seconds) or ttl_seconds <= 0):
            raise ValueError("ttl_seconds must be positive")
        if type(offline) is not bool:
            raise ValueError("offline must be bool")
        try:
            parsed = urlsplit(endpoint)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or parsed.query or parsed.fragment
                    or parsed.path != "/api/interpreter" or parsed.port == 0):
                raise ValueError("endpoint must be an HTTPS /api/interpreter URL without credentials or parameters")
        except (TypeError, ValueError) as exc:
            raise ValueError("endpoint must be an HTTPS /api/interpreter URL without credentials or parameters") from exc
        self.store = store
        self.cache_dir = Path(cache_dir)
        self.session = session if session is not None else requests.Session()
        self.ttl_seconds = ttl_seconds
        self.offline = offline
        self.endpoint = endpoint

    def collect(self, study_area: StudyArea) -> POICollection:
        """Fetch POI data for the given study area, using cache when valid."""
        if not isinstance(study_area, StudyArea):
            raise POIError("invalid_study_area")

        query = _build_query(study_area)
        # Cache key from (endpoint, query text, adapter version)
        key = hashlib.sha256(json.dumps([self.endpoint, query, ADAPTER_VERSION],
                                        sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        path = self.cache_dir / (key + ".json")
        url = self.endpoint
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
                pois, category_counts = _parse_elements(raw)
                if snapshot.area_id != study_area.area_id:
                    raise ValueError("cache area mismatch")
            except (ValueError, TypeError, KeyError, OSError, POIError) as exc:
                raise POIError("invalid_cache") from exc
            if expires > now:
                return POICollection(study_area, snapshot, len(pois), category_counts, True,
                                     checked)

        if self.offline:
            raise POIError("cache_expired" if previous else "cache_miss")

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        lock = self.cache_dir / "request.lock"
        token = uuid.uuid4().hex
        try:
            with lock.open("x", encoding="utf-8") as file:
                file.write(token)
        except FileExistsError as exc:
            raise POIError("request_locked") from exc

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
                    raise POIError("invalid_rate_state") from exc
                if remaining > 0:
                    time.sleep(remaining)

            observed = _now()
            _atomic_json(last_path, {"at": observed.isoformat()})

            try:
                with self.session.post(self.endpoint, data={"data": query},
                                       headers={"User-Agent": USER_AGENT},
                                       timeout=(10, 180), verify=True, stream=True,
                                       allow_redirects=False) as response:
                    if response.status_code in (403, 429):
                        raise POIError(
                            "access_denied" if response.status_code == 403 else "rate_limited",
                            details={"http_status": response.status_code},
                        )
                    if response.status_code != 200:
                        raise POIError("http_error", details={"http_status": response.status_code})
                    media_type = response.headers.get("Content-Type")
                    if media_type is not None and media_type.split(";", 1)[0].strip().lower() != "application/json":
                        raise POIError("invalid_response")
                    try:
                        length = int(response.headers.get("Content-Length", "0"))
                        if length < 0:
                            raise ValueError("negative length")
                    except (ValueError, TypeError) as exc:
                        raise POIError("invalid_response") from exc
                    if length > MAX_BYTES:
                        raise POIError("response_too_large")
                    data = bytearray()
                    for chunk in response.iter_content(chunk_size=65536):
                        data.extend(chunk)
                        if len(data) > MAX_BYTES:
                            raise POIError("response_too_large")
            except requests.exceptions.SSLError as exc:
                raise POIError("tls_error") from exc
            except requests.exceptions.Timeout as exc:
                raise POIError("timeout") from exc
            except requests.RequestException as exc:
                raise POIError("fetch_failed", details={"error_class": type(exc).__name__}) from exc

            pois, category_counts = _parse_elements(bytes(data))
            snapshot = self.store.save_snapshot(SOURCE_ID, study_area.area_id, url, bytes(data),
                                                ADAPTER_VERSION, "application/json", "complete",
                                                retrieved_at=observed)
            _atomic_json(path, {"schema_version": 1, "key": key,
                                "snapshot_id": snapshot.snapshot_id,
                                "checked_at": observed.isoformat(),
                                "expires_at": (observed + timedelta(seconds=self.ttl_seconds)).isoformat()})
            return POICollection(study_area, snapshot, len(pois), category_counts, False, observed)
        finally:
            if lock.read_text(encoding="utf-8") == token:
                lock.unlink()
