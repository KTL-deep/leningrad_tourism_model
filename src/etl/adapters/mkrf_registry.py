"""Bounded, keyless MKRF heritage register import; regional catalogue, not permits."""

from dataclasses import dataclass
import codecs
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit
import zipfile

import requests
from shapely.geometry import Point, shape

from src.data_models.study_area import StudyArea
from src.storage import SnapshotStore

SOURCE_URL = "https://opendata.mkrf.ru/opendata/7705851331-egrkn/"
SOURCE_ID = "mkrf_egrkn"
DATASET_ID = "57758409f0aecacd7fd18947"
PARSER_VERSION = "mkrf-egrkn-v2"
USER_AGENT = "LeningradTourismFramework/0.1 (+https://github.com/KTL-deep/leningrad_tourism_model)"
MAX_HTML = 4 * 1024 * 1024
MAX_SCHEMA = 2 * 1024 * 1024
MAX_COMPRESSED = 256 * 1024 * 1024
MAX_UNCOMPRESSED = 2 * 1024 * 1024 * 1024
MAX_RECORDS = 500000
MAX_LINE = 2 * 1024 * 1024
MAX_RETAINED = 50000
MAX_DERIVED = 64 * 1024 * 1024
DEADLINE = 1200
TTL = 86400
_BASE_PATH = urlsplit(SOURCE_URL).path


class MkrfError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise MkrfError("duplicate_json_key")
        result[key] = value
    return result


def _json(content):
    return json.loads(content, object_pairs_hook=_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(MkrfError("nonfinite_json")))


def _url(name, suffix):
    if not re.fullmatch(r"(?:data-\d+-structure-\d+|structure-\d+)", name):
        raise MkrfError("unsafe_download_name")
    if suffix not in ("json", "json.zip", "jsons.zip"):
        raise MkrfError("unsafe_format")
    return SOURCE_URL + name + "." + suffix


class _Passport(HTMLParser):
    def __init__(self):
        super().__init__()
        self.data = set()
        self.formats = {}
        self.schemas = set()
        self.dataset_ids = []

    def handle_starttag(self, tag, attrs):
        if tag == "input" and dict(attrs).get("id") == "dataset_id":
            self.dataset_ids.append(dict(attrs).get("value"))
        if tag != "div":
            return
        attrs = dict(attrs)
        if "download_btn" not in attrs.get("class", "").split():
            return
        name = attrs.get("data-name", "")
        formats = attrs.get("data-formats", "").split(",")
        if not name.startswith(("data-", "structure-")):
            return
        if attrs.get("data-url") != _BASE_PATH or len(formats) > 8:
            raise MkrfError("unsafe_passport_url")
        if name.startswith("data-"):
            _url(name, "jsons.zip")
            available = tuple(fmt for fmt in ("json.zip", "jsons.zip") if fmt in formats)
            if available:
                self.data.add(name)
                self.formats.setdefault(name, set()).update(available)
        else:
            _url(name, "json")
            if "json" in formats:
                self.schemas.add(name)


def _passport_details(content):
    if not content or len(content) > MAX_HTML:
        raise MkrfError("invalid_passport_size")
    parser = _Passport()
    try:
        parser.feed(content.decode("utf-8-sig"))
        parser.close()
    except UnicodeError as exc:
        raise MkrfError("invalid_passport_encoding") from exc
    if parser.dataset_ids != [DATASET_ID]:
        raise MkrfError("dataset_identity_mismatch")
    versions = sorted(((int(n), int(s), name) for name in parser.data
                       for n, s in [re.fullmatch(r"data-(\d+)-structure-(\d+)", name).groups()]),
                      reverse=True)
    if not versions:
        raise MkrfError("no_advertised_archive")
    latest = versions[0][0]
    candidates = [name for n, _, name in versions if n == latest][:2]
    if len([name for n, _, name in versions if n == latest]) > 2:
        raise MkrfError("too_many_current_structures")
    if any("structure-" + name.rsplit("-", 1)[-1] not in parser.schemas for name in candidates):
        raise MkrfError("missing_schema_advertisement")
    return latest, candidates, parser.formats


def _parse_passport(content):
    version, candidates, _ = _passport_details(content)
    return version, candidates


def discover(content):
    return _parse_passport(content)


def validate_schema(schema):
    """Check the schema paths consumed by this parser, not unrelated properties."""
    try:
        general = schema["properties"]["general"]
        properties = general["properties"]
        address = properties["address"]
        position = address["properties"]["mapPosition"]
        region = properties["region"]
        expected = ((schema, "object"), (general, "object"),
                    (properties["name"], "string"), (properties["regNumber"], "string"),
                    (region, "object"), (address, "object"), (position, "object"),
                    (position["properties"]["type"], "string"),
                    (position["properties"]["coordinates"], "array"))
        if any(not isinstance(node, dict) or node.get("type") != kind for node, kind in expected):
            raise MkrfError("unsupported_schema")
    except (TypeError, KeyError) as exc:
        raise MkrfError("unsupported_schema") from exc


def _record(raw, area_geom):
    if not isinstance(raw, dict) or not isinstance(raw.get("data"), dict):
        raise MkrfError("invalid_record")
    if raw.get("dataset") != DATASET_ID:
        raise MkrfError("dataset_identity_mismatch")
    schema_id = raw.get("odSchema")
    if not isinstance(schema_id, str) or not re.fullmatch(r"[0-9a-fA-F]{24}", schema_id):
        raise MkrfError("invalid_source_schema_id")
    general = raw["data"].get("general")
    if not isinstance(general, dict):
        raise MkrfError("invalid_record")
    region = general.get("region")
    if not isinstance(region, dict) or region.get("id") not in ("47", "78"):
        return None
    mongo_id, native_id, name = raw.get("_id"), raw.get("nativeId"), general.get("name")
    if (not isinstance(mongo_id, str) or not mongo_id.strip() or
            not isinstance(native_id, (str, int)) or isinstance(native_id, bool) or
            not str(native_id).strip() or not isinstance(name, str) or not name.strip()):
        raise MkrfError("invalid_record_identity")
    number = general.get("regNumber")
    if number is not None and not isinstance(number, str):
        raise MkrfError("invalid_reg_number_type")
    address = general.get("address")
    address = address if isinstance(address, dict) else {}
    position = address.get("mapPosition")
    geom, reason = None, "missing_point"
    if position is not None:
        coords = position.get("coordinates") if isinstance(position, dict) else None
        if (isinstance(position, dict) and position.get("type") == "Point"
                and isinstance(coords, list) and len(coords) == 2
                and all(type(n) in (int, float) and math.isfinite(n) for n in coords)
                and -180 <= coords[0] <= 180 and -90 <= coords[1] <= 90):
            geom, reason = {"type": "Point", "coordinates": coords}, None
        else:
            reason = "malformed_point"
    status = ("intersects_analysis" if area_geom.intersects(Point(*geom["coordinates"]))
              else "outside_analysis") if geom else "unlocated"
    return {"source_record_id": SOURCE_ID + ":" + mongo_id,
            "source_schema_id": schema_id, "reg_number": number,
            "registry_number_status": ("missing" if number is None else
                                       "valid_15_digits" if re.fullmatch(r"[0-9]{15}", number) else "invalid"),
            "native_id": native_id, "mongo_id": mongo_id, "general_id": general.get("id"),
            "name": name, "region_id": region["id"],
            "region_name": region.get("value"), "address": address.get("fullAddress"),
            "geometry": geom, "location_reason": reason, "spatial_status": status,
            "is_actual": general.get("isActual"), "category_type": general.get("categoryType"),
            "object_type": general.get("objectType"), "status": general.get("status"),
            "create_date": general.get("createDate")}


class _Records:
    def __init__(self, area):
        self.area_geom = shape(area.analysis_geometry or area.request_geometry)
        self.records = []
        self.ids = {"native_id": set(), "mongo_id": set()}
        self.reg_numbers = {}
        self.scanned = 0

    def add(self, raw):
        self.scanned += 1
        if self.scanned > MAX_RECORDS:
            raise MkrfError("archive_limit")
        record = _record(raw, self.area_geom)
        if record is None:
            return
        for key, seen in self.ids.items():
            value = record[key]
            if value is not None:
                identity = json.dumps(value, sort_keys=True, ensure_ascii=False)
                if identity in seen:
                    raise MkrfError("duplicate_" + key)
                seen.add(identity)
        number = record["reg_number"]
        if number is not None:
            self.reg_numbers.setdefault(number, []).append(len(self.records))
        self.records.append(record)
        if len(self.records) > MAX_RETAINED:
            raise MkrfError("retained_limit")

    def result(self):
        for indices in self.reg_numbers.values():
            if len(indices) > 1:
                for index in indices:
                    self.records[index]["registry_number_conflict"] = True
        for record in self.records:
            record.setdefault("registry_number_conflict", False)
        return self.records, self.scanned


def parse_archive(path, member_name, area):
    """Scan the entire JSONS ZIP member through EOF (including CRC)."""
    accumulator = _Records(area)
    start = time.monotonic()
    try:
        if Path(path).stat().st_size > MAX_COMPRESSED:
            raise MkrfError("oversized_archive")
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if (len(entries) != 1 or entries[0].filename != member_name or
                    entries[0].filename.startswith(("/", "\\")) or "\\" in entries[0].filename or
                    ".." in entries[0].filename.split("/") or
                    entries[0].compress_type != zipfile.ZIP_DEFLATED or
                    entries[0].file_size > MAX_UNCOMPRESSED or
                    entries[0].compress_size > MAX_COMPRESSED or entries[0].flag_bits & 1):
                raise MkrfError("unsafe_archive")
            total = 0
            with archive.open(entries[0]) as stream:
                while True:
                    if time.monotonic() - start > DEADLINE:
                        raise MkrfError("parser_deadline")
                    line = stream.readline(MAX_LINE + 1)
                    if not line:
                        break
                    total += len(line)
                    if total > MAX_UNCOMPRESSED or len(line) > MAX_LINE or accumulator.scanned >= MAX_RECORDS:
                        raise MkrfError("archive_limit")
                    if not line.endswith(b"\n"):
                        raise MkrfError("unterminated_line")
                    raw = _json(line)
                    accumulator.add(raw)
            if total != entries[0].file_size:
                raise MkrfError("archive_size_mismatch")
    except (zipfile.BadZipFile, EOFError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MkrfError("invalid_archive") from exc
    return accumulator.result()


def _array_member(stream, expected_size, accumulator, start):
    """Decode one top-level array, retaining at most one bounded record at a time."""
    decoder = json.JSONDecoder(object_pairs_hook=_pairs,
                               parse_constant=lambda _: (_ for _ in ()).throw(MkrfError("nonfinite_json")))
    utf8 = codecs.getincrementaldecoder("utf-8")()
    text, size, state, eof = "", 0, "open", False

    def fill():
        nonlocal text, size, eof
        if time.monotonic() - start > DEADLINE:
            raise MkrfError("parser_deadline")
        chunk = stream.read(65536)
        size += len(chunk)
        if size > expected_size or size > MAX_UNCOMPRESSED:
            raise MkrfError("archive_size_mismatch")
        eof = not chunk
        text += utf8.decode(chunk, final=eof)

    while True:
        if time.monotonic() - start > DEADLINE:
            raise MkrfError("parser_deadline")
        text = text.lstrip(" \t\r\n")
        if not text and not eof:
            fill()
            continue
        if state == "open":
            if not text or text[0] != "[":
                raise MkrfError("invalid_array")
            text, state = text[1:], "first"
        elif state in ("first", "value"):
            if state == "first" and text.startswith("]"):
                text, state = text[1:], "end"
                continue
            if not text or text[0] != "{":
                raise MkrfError("invalid_array")
            try:
                raw, index = decoder.raw_decode(text)
            except json.JSONDecodeError as exc:
                if eof:
                    raise MkrfError("invalid_array") from exc
                if len(text.encode("utf-8")) > MAX_LINE:
                    raise MkrfError("archive_limit") from exc
                fill()
                continue
            if len(text[:index].encode("utf-8")) > MAX_LINE:
                raise MkrfError("archive_limit")
            accumulator.add(raw)
            text, state = text[index:], "separator"
        elif state == "separator":
            if not text:
                raise MkrfError("invalid_array")
            if text[0] == ",":
                text, state = text[1:], "value"
            elif text[0] == "]":
                text, state = text[1:], "end"
            else:
                raise MkrfError("invalid_array")
        else:
            if text:
                raise MkrfError("invalid_array")
            if eof:
                if size != expected_size:
                    raise MkrfError("archive_size_mismatch")
                return
            fill()


def parse_json_archive(path, area):
    """Stream all numbered JSON-array members of an MKRF JSON ZIP export."""
    accumulator = _Records(area)
    start = time.monotonic()
    try:
        if Path(path).stat().st_size > MAX_COMPRESSED:
            raise MkrfError("oversized_archive")
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            names = {entry.filename for entry in entries}
            if (not 1 <= len(entries) <= 100 or
                    names != {f"{index}.json" for index in range(1, len(entries) + 1)} or
                    any(entry.compress_type != zipfile.ZIP_DEFLATED or entry.flag_bits & 1
                        for entry in entries) or
                    sum(entry.compress_size for entry in entries) > MAX_COMPRESSED or
                    sum(entry.file_size for entry in entries) > MAX_UNCOMPRESSED):
                raise MkrfError("unsafe_archive")
            for entry in sorted(entries, key=lambda item: int(item.filename[:-5])):
                with archive.open(entry) as stream:
                    _array_member(stream, entry.file_size, accumulator, start)
    except (zipfile.BadZipFile, EOFError, OSError, UnicodeError, json.JSONDecodeError,
            RecursionError) as exc:
        raise MkrfError("invalid_archive") from exc
    return accumulator.result()


def _fetch(session, url, limit, media, *, missing=False, file_path=None, deadline=None):
    if not url.startswith(SOURCE_URL) or urlsplit(url).hostname != "opendata.mkrf.ru":
        raise MkrfError("unsafe_url")
    try:
        if deadline is not None and time.monotonic() > deadline:
            raise MkrfError("network_deadline")
        with session.get(url, timeout=(10, 60), stream=True, verify=True,
                         allow_redirects=False, headers={"User-Agent": USER_AGENT}) as response:
            if deadline is not None and time.monotonic() > deadline:
                raise MkrfError("network_deadline")
            if response.status_code in (404, 410) and missing:
                return None
            if response.status_code != 200:
                raise MkrfError("http_" + str(response.status_code))
            if response.url != url:
                raise MkrfError("unsafe_redirect")
            if response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() not in media:
                raise MkrfError("invalid_media_type")
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdecimal() or int(length) > limit):
                raise MkrfError("oversized_response")
            size, body, digest = 0, bytearray(), hashlib.sha256()
            if file_path is None:
                for chunk in response.iter_content(chunk_size=65536):
                    if deadline is not None and time.monotonic() > deadline:
                        raise MkrfError("network_deadline")
                    size += len(chunk)
                    if size > limit:
                        raise MkrfError("oversized_response")
                    body.extend(chunk)
            else:
                with Path(file_path).open("wb") as output:
                    for chunk in response.iter_content(chunk_size=65536):
                        if deadline is not None and time.monotonic() > deadline:
                            raise MkrfError("network_deadline")
                        size += len(chunk)
                        if size > limit:
                            raise MkrfError("oversized_response")
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            if length is not None and size != int(length):
                raise MkrfError("truncated_response")
            if deadline is not None and time.monotonic() > deadline:
                raise MkrfError("network_deadline")
            return bytes(body) if file_path is None else (digest.hexdigest(), size)
    except requests.exceptions.SSLError as exc:
        raise MkrfError("tls_error") from exc
    except requests.exceptions.Timeout as exc:
        raise MkrfError("timeout") from exc
    except requests.RequestException as exc:
        raise MkrfError("network_error") from exc


def _encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                       separators=(",", ":")) + "\n").encode()


def _publish(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".pending-", delete=False) as output:
            pending = Path(output.name)
            output.write(_encoded(value))
            output.flush()
            os.fsync(output.fileno())
        os.replace(pending, path)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


@dataclass(frozen=True)
class MkrfResult:
    registry: dict
    passport_snapshot: object
    schema_snapshot: object
    archive_snapshot: object
    registry_snapshot: object
    from_cache: bool
    checked_at: datetime


class MkrfRegistryAdapter:
    def __init__(self, store: SnapshotStore, cache_dir: Path, session=None, offline=False):
        if type(offline) is not bool:
            raise TypeError("offline must be bool")
        self.store, self.cache_dir, self.offline = store, Path(cache_dir), offline
        self.session = session if session is not None else requests.Session()

    def collect(self, area: StudyArea) -> MkrfResult:
        if not isinstance(area, StudyArea):
            raise TypeError("area must be StudyArea")
        pointer_path = self.cache_dir / (hashlib.sha256(_encoded([SOURCE_URL, area.area_id, PARSER_VERSION])).hexdigest() + ".json")
        now = datetime.now(timezone.utc)
        if pointer_path.exists():
            try:
                pointer = _json(pointer_path.read_bytes())
                if set(pointer) != {"snapshots", "checked_at", "expires_at"} or set(pointer["snapshots"]) != {"passport", "schema", "archive", "registry"}:
                    raise ValueError("pointer fields")
                checked = datetime.fromisoformat(pointer["checked_at"])
                expiry = datetime.fromisoformat(pointer["expires_at"])
                if checked.tzinfo is None or expiry.tzinfo is None or checked > now or expiry != checked + timedelta(seconds=TTL):
                    raise ValueError("pointer time")
                if expiry <= now:
                    if self.offline:
                        raise MkrfError("cache_expired")
                    expired = True
                else:
                    expired = False
                snapshots = {k: self.store.verify_content(v) for k, v in pointer["snapshots"].items()}
                passport, schema, archive, derived = (snapshots[k] for k in ("passport", "schema", "archive", "registry"))
                if any(s.area_id != area.area_id or s.source_id != SOURCE_ID or s.parser_version != PARSER_VERSION or s.completeness != "partial" for s in snapshots.values()):
                    raise ValueError("snapshot identity")
                if (archive.byte_length > MAX_COMPRESSED or schema.byte_length > MAX_SCHEMA or
                        passport.byte_length > MAX_HTML or derived.byte_length > MAX_DERIVED):
                    raise ValueError("snapshot size")
                registry = _json(self.store.read_content(derived.snapshot_id))
                if (passport.url != SOURCE_URL or passport.media_type != "text/html" or
                        schema.media_type != "application/json" or archive.media_type != "application/zip" or
                        derived.url != archive.url or derived.media_type != "application/json" or
                        registry.get("archive_snapshot_id") != archive.snapshot_id or
                        registry.get("passport_snapshot_id") != passport.snapshot_id or
                        registry.get("schema_snapshot_id") != schema.snapshot_id or
                        registry.get("area_id") != area.area_id or
                        registry.get("source_versions", {}).get("archive_url") != archive.url or
                        registry.get("source_versions", {}).get("schema_url") != schema.url):
                    raise ValueError("derived identity")
                validate_schema(_json(self.store.read_content(schema.snapshot_id)))
                version, candidates, formats = _passport_details(self.store.read_content(passport.snapshot_id))
                source = registry["source_versions"]
                choices = [(n, fmt) for n in candidates for fmt in ("json.zip", "jsons.zip") if fmt in formats[n]]
                if (not any(archive.url == _url(n, fmt) and source.get("archive_format") == fmt
                            and source.get("archive_structure") == int(n.rsplit("-", 1)[-1])
                            for n, fmt in choices) or
                        schema.url != _url("structure-" + archive.url.rsplit("-", 1)[-1].split(".")[0], "json") or
                        source["data_version"] != version):
                    raise ValueError("source version mismatch")
                if not expired:
                    return MkrfResult(registry, passport, schema, archive, derived, True, checked)
            except MkrfError as exc:
                if exc.code == "cache_expired":
                    raise
                raise MkrfError("cache_corrupt") from exc
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                raise MkrfError("cache_corrupt") from exc
        elif self.offline:
            raise MkrfError("cache_missing")
        deadline = time.monotonic() + DEADLINE
        passport_bytes = _fetch(self.session, SOURCE_URL, MAX_HTML, {"text/html"}, deadline=deadline)
        version, candidates, formats = _passport_details(passport_bytes)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        pending = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.cache_dir, prefix=".mkrf-", delete=False) as output:
                pending = Path(output.name)
            for name in candidates:
                for fmt in ("json.zip", "jsons.zip"):
                    if fmt not in formats[name]:
                        continue
                    archive_url = _url(name, fmt)
                    download = _fetch(self.session, archive_url, MAX_COMPRESSED, {"application/zip", "application/x-zip-compressed"}, missing=True, file_path=pending, deadline=deadline)
                    if download is not None:
                        break
                if download is not None:
                    break
            else:
                raise MkrfError("advertised_archive_unavailable")
            schema_url = _url("structure-" + name.rsplit("-", 1)[-1], "json")
            schema_bytes = _fetch(self.session, schema_url, MAX_SCHEMA, {"application/json"}, deadline=deadline)
            validate_schema(_json(schema_bytes))
            records, scanned = (parse_json_archive(pending, area) if fmt == "json.zip" else
                                parse_archive(pending, name + ".jsons", area))
            if time.monotonic() > deadline:
                raise MkrfError("parser_deadline")
            passport = self.store.save_snapshot(SOURCE_ID, area.area_id, SOURCE_URL, passport_bytes, PARSER_VERSION, "text/html", "partial")
            schema = self.store.save_snapshot(SOURCE_ID, area.area_id, schema_url, schema_bytes, PARSER_VERSION, "application/json", "partial")
            archive = self.store.save_snapshot_file(SOURCE_ID, area.area_id, archive_url, pending, PARSER_VERSION,
                                                    "application/zip", "partial", expected_hash=download[0],
                                                    expected_size=download[1])
            registry = {"schema_version": 1, "parser_version": PARSER_VERSION, "source_id": SOURCE_ID,
                        "area_id": area.area_id, "source_url": SOURCE_URL,
                        "passport_snapshot_id": passport.snapshot_id, "schema_snapshot_id": schema.snapshot_id,
                        "archive_snapshot_id": archive.snapshot_id,
                        "source_versions": {"data_version": version, "archive_structure": int(name.rsplit("-", 1)[-1]),
                                            "archive_format": fmt,
                                            "archive_url": archive_url, "schema_url": schema_url,
                                            "advertised_candidates": [_url(n, kind) for n in candidates
                                                                      for kind in ("json.zip", "jsons.zip")
                                                                      if kind in formats[n]]},
                        "source_reported_date": None,
                        "schema_numeric_to_source_id_mapping": "unresolved",
                        "terms_reference": SOURCE_URL, "attribution": "Министерство культуры Российской Федерации, ЕГРОКН",
                        "completeness": "partial", "coverage": "regional_catalog_47_78_not_area_filtered",
                        "coordinate_order_warning": "GeoJSON Point coordinates treated as longitude, latitude; source schema also labels lat,long; unverified point is neither boundary nor entrance.",
                        "interpretation_warning": "Registry listing is not a construction permit or a capacity estimate.",
                        "quality": {"scanned_records": scanned, "retained_records": len(records),
                                    "unlocated": sum(r["spatial_status"] == "unlocated" for r in records),
                                    "outside_analysis": sum(r["spatial_status"] == "outside_analysis" for r in records),
                                    "observed_source_schema_ids": sorted({r["source_schema_id"] for r in records}),
                                    "duplicate_registry_numbers": len({r["reg_number"] for r in records if r["registry_number_conflict"]})},
                        "records": records}
            encoded = _encoded(registry)
            if len(encoded) > MAX_DERIVED:
                raise MkrfError("derived_limit")
            derived = self.store.save_snapshot(SOURCE_ID, area.area_id, archive_url, encoded, PARSER_VERSION, "application/json", "partial")
            checked = datetime.now(timezone.utc)
            _publish(pointer_path, {"snapshots": {"passport": passport.snapshot_id, "schema": schema.snapshot_id,
                                                    "archive": archive.snapshot_id, "registry": derived.snapshot_id},
                                    "checked_at": checked.isoformat(), "expires_at": (checked + timedelta(seconds=TTL)).isoformat()})
            return MkrfResult(registry, passport, schema, archive, derived, False, checked)
        finally:
            if pending is not None:
                pending.unlink(missing_ok=True)
