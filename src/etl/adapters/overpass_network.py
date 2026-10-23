"""Overpass API adapter for OSM road/path network within a study area boundary.

Queries ways (highway, railway) and relations (route) inside the study area
polygon, stores the raw JSON response as an immutable snapshot, and returns
parsed network statistics.
"""

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

from src.data_models.study_area import StudyArea
from src.storage import SnapshotStore


ENDPOINT = "https://overpass-api.de/api/interpreter"
SOURCE_ID = "osm_overpass_network"
ADAPTER_VERSION = "overpass-network-v1"
USER_AGENT = "LeningradTourismFramework/0.1 (+https://github.com/KTL-deep/leningrad_tourism_model)"
ATTRIBUTION = "© OpenStreetMap contributors (ODbL)"
MAX_BYTES = 100 * 1024 * 1024
INTERVAL = 1.1

HIGHWAY_VALUES = (
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "unclassified", "residential", "living_street", "service",
    "pedestrian", "footway", "cycleway", "path", "track",
)
RAILWAY_VALUES = ("rail", "light_rail", "narrow_gauge", "station", "halt")
ROUTE_VALUES = ("bus", "trolleybus", "tram")


class NetworkError(Exception):
    """Typed error raised by the Overpass network adapter."""

    def __init__(self, code: str, details=None):
        self.code = code
        self.details = details
        super().__init__(code)


@dataclass(frozen=True)
class NetworkCollection:
    """Result of an Overpass network query."""

    study_area: StudyArea
    snapshot: object
    way_count: int
    station_count: int
    route_count: int
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


def _polygon_coords(study_area: StudyArea) -> str:
    """Build Overpass ``poly:"lat1 lon1 ..."`` coordinate string from the study area.

    Uses *analysis_geometry* when available, falling back to *request_geometry*.
    """
    geometry = study_area.analysis_geometry if study_area.analysis_geometry is not None else study_area.request_geometry
    geom_type = geometry["type"]
    coordinates = geometry["coordinates"]

    # Collect all exterior ring coordinates (first ring of each polygon).
    if geom_type == "Polygon":
        rings = [coordinates[0]]
    else:  # MultiPolygon
        rings = [polygon[0] for polygon in coordinates]

    # Overpass expects "lat1 lon1 lat2 lon2 ..." (note: lat before lon).
    parts = []
    for ring in rings:
        for lon, lat in ring:
            parts.append(f"{lat} {lon}")
    return " ".join(parts)


def build_query(study_area: StudyArea) -> str:
    """Construct the Overpass QL query for road/transport network."""
    poly = _polygon_coords(study_area)

    highway_re = "|".join(HIGHWAY_VALUES)
    railway_re = "|".join(RAILWAY_VALUES)
    route_re = "|".join(ROUTE_VALUES)

    # Stations are nodes with railway=station|halt.
    station_railway = "station|halt"

    query = (
        f'[out:json][timeout:180];\n'
        f'(\n'
        f'  way["highway"~"^({highway_re})$"](poly:"{poly}");\n'
        f'  way["railway"~"^({railway_re})$"](poly:"{poly}");\n'
        f'  node["railway"~"^({station_railway})$"](poly:"{poly}");\n'
        f'  relation["route"~"^({route_re})$"](poly:"{poly}");\n'
        f');\n'
        f'out geom;\n'
    )
    return query


def _parse_response(content: bytes) -> tuple[int, int, int]:
    """Parse the Overpass JSON response and return (way_count, station_count, route_count)."""
    try:
        data = json.loads(content.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise NetworkError("invalid_response") from exc

    if not isinstance(data, dict) or "elements" not in data:
        raise NetworkError("invalid_response")

    elements = data["elements"]
    if not isinstance(elements, list):
        raise NetworkError("invalid_response")

    way_count = 0
    station_count = 0
    route_count = 0

    for element in elements:
        if not isinstance(element, dict):
            raise NetworkError("invalid_response")

        etype = element.get("type")
        tags = element.get("tags", {})
        if not isinstance(tags, dict):
            raise NetworkError("invalid_response")

        if etype == "way":
            # Ways with geometry should have a "geometry" array of {lat, lon} objects.
            geometry = element.get("geometry")
            if not isinstance(geometry, list) or len(geometry) < 2:
                continue  # skip degenerate ways
            valid = True
            for node in geometry:
                if not isinstance(node, dict) or "lat" not in node or "lon" not in node:
                    valid = False
                    break
            if not valid:
                continue
            way_count += 1

        elif etype == "node":
            # Station nodes should have lat/lon and railway=station|halt.
            railway = tags.get("railway")
            if railway in ("station", "halt") and "lat" in element and "lon" in element:
                station_count += 1

        elif etype == "relation":
            route = tags.get("route")
            if route in ROUTE_VALUES:
                route_count += 1

    return way_count, station_count, route_count


class OverpassNetworkAdapter:
    """Fetch and cache OSM road/path network data via the Overpass API."""

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
                    or parsed.port == 0):
                raise ValueError("endpoint must be an HTTPS URL without credentials or parameters")
        except (TypeError, ValueError) as exc:
            raise ValueError("endpoint must be an HTTPS URL without credentials or parameters") from exc
        self.store = store
        self.cache_dir = Path(cache_dir)
        self.session = session if session is not None else requests.Session()
        self.ttl_seconds = ttl_seconds
        self.offline = offline
        self.endpoint = endpoint

    def collect(self, study_area: StudyArea) -> NetworkCollection:
        """Query the Overpass API for network data within the study area."""
        if not isinstance(study_area, StudyArea):
            raise NetworkError("invalid_study_area")

        query = build_query(study_area)
        post_data = {"data": query}

        # Cache key incorporates the query, endpoint, and adapter version.
        key = hashlib.sha256(json.dumps(
            [self.endpoint, query, ADAPTER_VERSION],
            sort_keys=True, ensure_ascii=False,
        ).encode("utf-8")).hexdigest()

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
                        or snapshot.completeness != "complete"):
                    raise ValueError("cache provenance mismatch")
                raw = self.store.read_content(snapshot.snapshot_id)
                if len(raw) > MAX_BYTES:
                    raise ValueError("oversized cached response")
                way_count, station_count, route_count = _parse_response(raw)
            except (ValueError, TypeError, KeyError, OSError, NetworkError) as exc:
                raise NetworkError("invalid_cache") from exc

            if expires > now:
                return NetworkCollection(
                    study_area=study_area,
                    snapshot=snapshot,
                    way_count=way_count,
                    station_count=station_count,
                    route_count=route_count,
                    from_cache=True,
                    observed_at=checked,
                )

        if self.offline:
            raise NetworkError("cache_expired" if previous else "cache_miss")

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        lock = self.cache_dir / "request.lock"
        token = uuid.uuid4().hex
        try:
            with lock.open("x", encoding="utf-8") as file:
                file.write(token)
        except FileExistsError as exc:
            raise NetworkError("request_locked") from exc

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
                    raise NetworkError("invalid_rate_state") from exc
                if remaining > 0:
                    time.sleep(remaining)

            observed = _now()
            _atomic_json(last_path, {"at": observed.isoformat()})

            url = self.endpoint
            try:
                with self.session.post(
                    url, data=post_data,
                    headers={"User-Agent": USER_AGENT},
                    timeout=(10, 180), verify=True, stream=True,
                    allow_redirects=False,
                ) as response:
                    if response.status_code in (403, 429):
                        raise NetworkError(
                            "access_denied" if response.status_code == 403 else "rate_limited",
                            details={"http_status": response.status_code},
                        )
                    if response.status_code != 200:
                        raise NetworkError("http_error", details={"http_status": response.status_code})
                    media_type = response.headers.get("Content-Type")
                    if media_type is not None and media_type.split(";", 1)[0].strip().lower() != "application/json":
                        raise NetworkError("invalid_response")
                    try:
                        length = int(response.headers.get("Content-Length", "0"))
                        if length < 0:
                            raise ValueError("negative length")
                    except (ValueError, TypeError) as exc:
                        raise NetworkError("invalid_response") from exc
                    if length > MAX_BYTES:
                        raise NetworkError("response_too_large")
                    data = bytearray()
                    for chunk in response.iter_content(chunk_size=65536):
                        data.extend(chunk)
                        if len(data) > MAX_BYTES:
                            raise NetworkError("response_too_large")
            except requests.exceptions.SSLError as exc:
                raise NetworkError("tls_error") from exc
            except requests.exceptions.Timeout as exc:
                raise NetworkError("timeout") from exc
            except requests.RequestException as exc:
                raise NetworkError("fetch_failed", details={"error_class": type(exc).__name__}) from exc

            raw = bytes(data)
            way_count, station_count, route_count = _parse_response(raw)

            snapshot = self.store.save_snapshot(
                SOURCE_ID, study_area.area_id, url, raw,
                ADAPTER_VERSION, "application/json", "complete",
                retrieved_at=observed,
            )

            _atomic_json(path, {
                "schema_version": 1, "key": key,
                "snapshot_id": snapshot.snapshot_id,
                "checked_at": observed.isoformat(),
                "expires_at": (observed + timedelta(seconds=self.ttl_seconds)).isoformat(),
            })

            return NetworkCollection(
                study_area=study_area,
                snapshot=snapshot,
                way_count=way_count,
                station_count=station_count,
                route_count=route_count,
                from_cache=False,
                observed_at=observed,
            )
        finally:
            if lock.read_text(encoding="utf-8") == token:
                lock.unlink()
