"""Immutable, caller-defined study area; jurisdiction claims are caller attestations.

OSM geometry alone does not establish legal jurisdiction boundaries. ``verified``
records the caller's affirmation, not a legal certification by this model.
"""

from dataclasses import dataclass, field
from hashlib import sha256
import json
import math

from pyproj import CRS
from pyproj.exceptions import CRSError
from shapely import normalize
from shapely.geometry import mapping, shape


def _geometry(value: dict, label: str) -> tuple[str, object]:
    if not isinstance(value, dict) or value.get("type") not in ("Polygon", "MultiPolygon"):
        raise ValueError(f"{label} must be a Polygon or MultiPolygon")
    if "crs" in value:
        raise ValueError(f"{label} embedded CRS is not supported; use explicit EPSG:4326")
    coordinates = value.get("coordinates")

    def sequence(items):
        return isinstance(items, (list, tuple)) and bool(items)

    polygons = [coordinates] if value["type"] == "Polygon" else coordinates
    if not sequence(polygons):
        raise ValueError(f"{label} has malformed coordinates")
    for polygon in polygons:
        if not sequence(polygon):
            raise ValueError(f"{label} has malformed rings")
        for ring in polygon:
            if not isinstance(ring, (list, tuple)) or len(ring) < 4:
                raise ValueError(f"{label} has a short ring")
            for point in ring:
                if not isinstance(point, (list, tuple)) or len(point) != 2:
                    raise ValueError(f"{label} requires 2D coordinates")
                lon, lat = point
                if any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n)
                       for n in point) or not (-180 <= lon <= 180 and -90 <= lat <= 90):
                    raise ValueError(f"{label} coordinates must be finite WGS84 longitude/latitude")
            if tuple(ring[0]) != tuple(ring[-1]):
                raise ValueError(f"{label} has an unclosed ring")
    try:
        geom = shape(value)
    except (ValueError, TypeError, IndexError) as exc:
        raise ValueError(f"{label} has malformed coordinates") from exc
    if geom.is_empty or not geom.is_valid or geom.geom_type not in ("Polygon", "MultiPolygon"):
        raise ValueError(f"{label} must be nonempty and valid")
    canonical = json.dumps(mapping(normalize(geom)), sort_keys=True, separators=(",", ":"))
    return canonical, geom


@dataclass(frozen=True, slots=True, init=False)
class StudyArea:
    name: str
    crs: str
    jurisdictions: tuple[str, ...]
    jurisdiction_status: str
    metric_crs: str | None
    area_id: str
    _request_json: str = field(repr=False)
    _analysis_json: str | None = field(repr=False)

    def __init__(
        self, name: str, request_geometry: dict, crs: str = "EPSG:4326",
        jurisdictions: tuple[str, ...] = (), jurisdiction_status: str = "unknown",
        analysis_geometry: dict | None = None, metric_crs: str | None = None,
    ) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name must be nonempty text")
        if crs != "EPSG:4326":
            raise ValueError("Only EPSG:4326 input is supported")
        if not isinstance(jurisdictions, tuple) or any(
            not isinstance(j, str) or not j.strip() for j in jurisdictions
        ) or len(jurisdictions) != len(set(jurisdictions)):
            raise ValueError("jurisdictions must be a tuple of unique nonempty names")
        if jurisdiction_status not in ("unknown", "source_reported", "verified"):
            raise ValueError("Invalid jurisdiction_status")
        if not jurisdictions and jurisdiction_status != "unknown":
            raise ValueError("Jurisdiction status without jurisdictions must be unknown")
        if metric_crs is not None:
            if not isinstance(metric_crs, str) or not metric_crs.strip():
                raise ValueError("metric_crs must be nonempty text")
            try:
                metric = CRS.from_user_input(metric_crs)
            except (ValueError, TypeError, CRSError) as exc:
                raise ValueError("Invalid metric_crs") from exc
            if not metric.is_projected or len(metric.axis_info) < 2 or any(
                axis.unit_name.lower() not in ("metre", "meter", "metres", "meters")
                or not math.isclose(axis.unit_conversion_factor, 1.0)
                for axis in metric.axis_info[:2]
            ):
                raise ValueError("metric_crs must have projected metre axes")
        request_json, request = _geometry(request_geometry, "request_geometry")
        analysis_json = None
        if analysis_geometry is not None:
            analysis_json, analysis = _geometry(analysis_geometry, "analysis_geometry")
            if not analysis.covers(request):
                raise ValueError("analysis_geometry must cover request_geometry")
        scope = (request_json, analysis_json, crs, jurisdictions, jurisdiction_status, metric_crs)
        area_id = sha256(json.dumps(scope, separators=(",", ":")).encode("utf-8")).hexdigest()
        for key, value in (
            ("name", name), ("crs", crs), ("jurisdictions", jurisdictions),
            ("jurisdiction_status", jurisdiction_status), ("metric_crs", metric_crs),
            ("area_id", area_id), ("_request_json", request_json), ("_analysis_json", analysis_json),
        ):
            object.__setattr__(self, key, value)

    @property
    def request_geometry(self) -> dict:
        return json.loads(self._request_json)

    @property
    def analysis_geometry(self) -> dict | None:
        return json.loads(self._analysis_json) if self._analysis_json is not None else None

    def to_dict(self) -> dict:
        return {
            "schema_version": 1, "name": self.name, "area_id": self.area_id,
            "request_geometry": self.request_geometry, "analysis_geometry": self.analysis_geometry,
            "crs": self.crs, "jurisdictions": list(self.jurisdictions),
            "jurisdiction_status": self.jurisdiction_status, "metric_crs": self.metric_crs,
            "surrounding_status": "explicit" if self._analysis_json is not None else "not_configured",
        }

    @classmethod
    def from_dict(cls, data: dict) -> "StudyArea":
        required = {
            "schema_version", "name", "area_id", "request_geometry", "analysis_geometry",
            "crs", "jurisdictions", "jurisdiction_status", "metric_crs", "surrounding_status",
        }
        if not isinstance(data, dict) or set(data) != required:
            raise ValueError("Invalid StudyArea record")
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise ValueError("Unsupported StudyArea schema version")
        if not isinstance(data["jurisdictions"], list):
            raise ValueError("Serialized jurisdictions must be a list")
        area = cls(
            name=data["name"], request_geometry=data["request_geometry"], crs=data["crs"],
            jurisdictions=tuple(data["jurisdictions"]), jurisdiction_status=data["jurisdiction_status"],
            analysis_geometry=data["analysis_geometry"], metric_crs=data["metric_crs"],
        )
        if area.area_id != data["area_id"] or area.to_dict()["surrounding_status"] != data["surrounding_status"]:
            raise ValueError("StudyArea identity or surrounding status mismatch")
        return area
