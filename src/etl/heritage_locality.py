"""Conservative address-locality envelope; never a heritage or legal boundary."""

from dataclasses import dataclass
from datetime import datetime
import json
import math
import re
import unicodedata

from shapely.geometry import shape

from src.data_models.contracts import SourceSnapshot
from src.data_models.study_area import StudyArea
from src.etl.adapters.nominatim_boundary import (
    ADAPTER_VERSION, SOURCE_ID as NOMINATIM_SOURCE_ID, BoundaryError, MAX_BYTES,
    NominatimBoundaryAdapter,
)
from src.storage import SnapshotStore


SOURCE_ID = "heritage_locality_boundary"
PARSER_VERSION = "heritage-locality-context-v1"
GUARD_M = 5000
INTERPRETATION = "locality_envelope_not_heritage_geometry_or_legal_boundary"
_KEYS = {"context_version", "source_snapshot_id", "original_snapshot_id", "raw_sha256",
         "locality_name", "region_name", "query", "status", "bbox", "guard_m", "interpretation"}


class LocalityError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _normalized(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("name must be text")
    value = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.sub(r"^(?:г\.|город\s+)", "", value.strip()).split())


def _name(value: str) -> bool:
    return bool(value) and all(character.isalpha() or character in " -" for character in value) and any(
        character.isalpha() for character in value
    ) and not value.startswith((" ", "-")) and not value.endswith((" ", "-"))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite JSON number")
    return number


def _invalid_constant(value):
    raise ValueError("nonfinite JSON constant")


def extract_address_locality(address, region_name) -> dict | None:
    """Only explicitly prefixed, comma-separated city components qualify."""
    if (not isinstance(address, str) or not isinstance(region_name, str)
            or not address.strip() or len(address) > 8192 or len(region_name) > 512 or ";" in address
            or any(unicodedata.category(c).startswith("C") for c in address + region_name)
            or not _name(_normalized(region_name))):
        return None
    cities = []
    for component in address.split(","):
        component = unicodedata.normalize("NFKC", component).strip()
        match = re.match(r"^(?:г\.\s*|город\s+)(.*)$", component, re.IGNORECASE)
        if match:
            city = " ".join(match.group(1).split())
            if len(city) > 256 or not _name(city):
                return None
            cities.append(city)
    if not cities:
        return None
    if len(cities) == 1:
        city = cities[0]
    elif len(cities) == 2 and _normalized(cities[0]) == _normalized(region_name):
        city = cities[1]
    else:
        return None
    region = " ".join(re.sub(r"^(?:г\.\s*|город\s+)", "", unicodedata.normalize(
        "NFKC", region_name).strip(), flags=re.IGNORECASE).split())
    return {"locality_name": city, "region_name": region, "query": f"{city}, {region}, Россия"}


@dataclass(frozen=True)
class LocalityResolution:
    context: dict
    snapshot: SourceSnapshot
    from_cache: bool
    checked_at: datetime


class HeritageLocalityResolver:
    def __init__(self, store: SnapshotStore, cache_dir, session=None, offline=False):
        self.store = store
        self.adapter = NominatimBoundaryAdapter(store, cache_dir, session=session, offline=offline)

    def resolve(self, area: StudyArea, locality_name: str, region_name: str) -> LocalityResolution:
        if (not isinstance(area, StudyArea) or not isinstance(locality_name, str)
                or not isinstance(region_name, str) or len(locality_name) > 256
                or len(region_name) > 512 or not _name(_normalized(locality_name))
                or not _name(_normalized(region_name))):
            raise LocalityError("invalid_locality")
        query = f"{locality_name}, {region_name}, Россия"
        try:
            collected = self.adapter.collect(query)
        except BoundaryError as exc:
            raise LocalityError(exc.code) from exc
        original = collected.snapshot
        try:
            verified = self.store.get_snapshot(original.snapshot_id)
            if (verified != original or verified.byte_length > MAX_BYTES
                    or verified.source_id != NOMINATIM_SOURCE_ID
                    or verified.parser_version != ADAPTER_VERSION
                    or verified.area_id != collected.study_area.area_id):
                raise ValueError("original snapshot provenance mismatch")
            raw = self.store.read_content(original.snapshot_id)
        except (OSError, ValueError) as exc:
            raise LocalityError("invalid_snapshot") from exc
        if len(raw) > MAX_BYTES:
            raise LocalityError("response_too_large")
        try:
            records = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                                 parse_float=_finite_float, parse_constant=_invalid_constant)
            if not isinstance(records, list) or len(records) > 5:
                raise ValueError("invalid records")
            candidates = {}
            for record in records:
                if not isinstance(record, dict):
                    raise ValueError("invalid record")
                geometry = record.get("geojson")
                if (record.get("category", record.get("class")) not in ("place", "boundary")
                        or record.get("type") not in ("city", "town", "village", "administrative", "municipality", "suburb")
                        or not isinstance(geometry, dict)
                        or geometry.get("type") not in ("Polygon", "MultiPolygon")):
                    continue
                identity = (record.get("osm_type"), record.get("osm_id"))
                if identity[0] not in ("way", "relation") or type(identity[1]) is not int or identity[1] <= 0:
                    raise ValueError("invalid identity")
                if identity in candidates and candidates[identity] != record:
                    raise LocalityError("ambiguous_locality")
                candidates[identity] = record
            if len(candidates) != 1:
                raise LocalityError("ambiguous_locality" if candidates else "not_found")
            selected = next(iter(candidates.values()))
            address = selected.get("address")
            if not isinstance(address, dict):
                raise ValueError("missing address")
            if address.get("country_code") != "ru" or _normalized(address.get("state", "")) != _normalized(region_name):
                raise LocalityError("wrong_region")
            if address.get("addresstype", selected.get("addresstype")) not in ("city", "town", "village"):
                raise LocalityError("wrong_locality_type")
            names = [selected.get("name"), *(address.get(key) for key in ("city", "town", "village"))]
            if not any(isinstance(name, str) and _normalized(name) == _normalized(locality_name) for name in names):
                raise LocalityError("wrong_locality")
            polygon = StudyArea(name=locality_name, request_geometry=selected["geojson"])
            if polygon.request_geometry != collected.study_area.request_geometry:
                raise LocalityError("geometry_mismatch")
            minlon, minlat, maxlon, maxlat = shape(polygon.request_geometry).bounds
            if not minlon < maxlon or not minlat < maxlat:
                raise ValueError("degenerate envelope")
        except (UnicodeError, ValueError, TypeError, KeyError, OverflowError) as exc:
            if isinstance(exc, LocalityError):
                raise
            raise LocalityError("invalid_response") from exc
        rebound = self.store.save_snapshot(SOURCE_ID, area.area_id, original.url, raw,
                                           PARSER_VERSION, "application/json", "partial",
                                           retrieved_at=original.retrieved_at)
        context = {"context_version": 1, "source_snapshot_id": rebound.snapshot_id,
                   "original_snapshot_id": original.snapshot_id, "raw_sha256": rebound.content_hash,
                   "locality_name": locality_name, "region_name": region_name, "query": query,
                   "status": "source_reported", "bbox": [minlon, minlat, maxlon, maxlat],
                   "guard_m": GUARD_M, "interpretation": INTERPRETATION}
        return LocalityResolution(context, rebound, collected.from_cache, collected.observed_at)


def validate_locality_context(context) -> dict:
    if not isinstance(context, dict) or set(context) != _KEYS:
        raise LocalityError("invalid_context")
    if (type(context["context_version"]) is not int or context["context_version"] != 1
            or type(context["guard_m"]) is not int or context["guard_m"] != GUARD_M
            or context["status"] != "source_reported" or context["interpretation"] != INTERPRETATION):
        raise LocalityError("invalid_context")
    for key in ("source_snapshot_id", "original_snapshot_id", "locality_name", "region_name", "query"):
        if not isinstance(context[key], str) or not context[key].strip():
            raise LocalityError("invalid_context")
    if not isinstance(context["raw_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", context["raw_sha256"]):
        raise LocalityError("invalid_context")
    bbox = context["bbox"]
    if (not isinstance(bbox, (list, tuple)) or len(bbox) != 4
            or any(type(n) not in (int, float) or not math.isfinite(n) for n in bbox)
            or not (-180 <= bbox[0] < bbox[2] <= 180 and -90 <= bbox[1] < bbox[3] <= 90)):
        raise LocalityError("invalid_context")
    return {**context, "bbox": list(bbox)}


def compare_locality_context(context, point) -> dict:
    validated = validate_locality_context(context)
    result = {"status": "unknown_geometry", "guard_m": GUARD_M,
              "interpretation": INTERPRETATION}
    if point is None:
        return result
    if isinstance(point, dict) and set(point) >= {"lon", "lat"}:
        lon, lat = point["lon"], point["lat"]
    elif isinstance(point, (tuple, list)) and len(point) == 2:
        lon, lat = point
    else:
        raise LocalityError("invalid_point")
    if (type(lon) not in (int, float) or type(lat) not in (int, float)
            or not math.isfinite(lon) or not math.isfinite(lat)
            or not (-180 <= lon <= 180 and -90 <= lat <= 90)):
        raise LocalityError("invalid_point")
    minlon, minlat, maxlon, maxlat = validated["bbox"]
    lat_margin = GUARD_M / 100000
    extreme_lat = max(abs(minlat - lat_margin), abs(maxlat + lat_margin), abs(lat))
    if extreme_lat >= 85 or maxlon - minlon > 180:
        return result
    lon_margin = GUARD_M / (100000 * math.cos(math.radians(extreme_lat)))
    if lon_margin >= 180 or minlon - lon_margin < -180 or maxlon + lon_margin > 180:
        return result
    result["status"] = ("inside_or_near_context_envelope"
                        if minlat - lat_margin <= lat <= maxlat + lat_margin
                        and minlon - lon_margin <= lon <= maxlon + lon_margin
                        else "outside_guarded_context_envelope")
    return result
