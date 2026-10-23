"""Bounded, verified Geofabrik extract retrieval; its .poly is coverage, not jurisdiction."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time
import re
import uuid
from urllib.parse import urlsplit

import requests
from shapely.geometry import Polygon
from shapely.ops import unary_union

from src.etl.adapters.overpass_inventory import collection_bbox


URL = "https://download.geofabrik.de/russia/northwestern-fed-district-latest.osm.pbf"
POLY_URL = "https://download.geofabrik.de/russia/northwestern-fed-district.poly"
USER_AGENT = "LeningradTourismFramework/0.1 (+https://github.com/KTL-deep/leningrad_tourism_model)"
MAX_PBF = 768 * 1024 * 1024
MAX_POLY = 4 * 1024 * 1024
ATTRIBUTION = "© OpenStreetMap contributors (ODbL); Geofabrik"


class ExtractError(Exception):
    def __init__(self, code, details=None):
        self.code, self.details = code, details
        super().__init__(code)


def coverage_polygon(content):
    """Parse Geofabrik .poly rings, including subtractive (!) rings, fail closed."""
    try:
        lines = content.decode("utf-8-sig").splitlines()
        if len(lines) < 5 or not lines[0].strip():
            raise ValueError("empty poly")
        index, shells, holes = 1, [], []
        while index < len(lines) and lines[index].strip() != "END":
            label = lines[index].strip()
            if not label or label.startswith("!") and len(label) == 1:
                raise ValueError("invalid ring")
            index += 1
            coords = []
            while index < len(lines) and lines[index].strip() != "END":
                parts = lines[index].split()
                if len(parts) != 2:
                    raise ValueError("invalid point")
                lon, lat = map(float, parts)
                if not all(map(math.isfinite, (lon, lat))) or not (-180 <= lon <= 180 and -90 <= lat <= 90):
                    raise ValueError("invalid coordinate")
                coords.append((lon, lat))
                index += 1
            if index == len(lines) or len(coords) < 3:
                raise ValueError("short ring")
            if coords[0] != coords[-1]:
                coords.append(coords[0])  # Osmosis .poly permits implicit ring closure.
            ring = Polygon(coords)
            if not ring.is_valid or ring.is_empty or ring.area == 0:
                raise ValueError("invalid ring")
            (holes if label.startswith("!") else shells).append(ring)
            index += 1
        if index != len(lines) - 1 or not shells:
            raise ValueError("invalid poly terminator")
        result = unary_union(shells)
        for hole in holes:
            if not result.covers(hole):
                raise ValueError("hole outside coverage")
            result = result.difference(hole)
        if result.is_empty or not result.is_valid:
            raise ValueError("invalid coverage")
        return result
    except (UnicodeError, ValueError, TypeError) as exc:
        raise ExtractError("invalid_poly") from exc


class GeofabrikExtractAdapter:
    def __init__(self, cache_dir, *, session=None, offline=False, ttl_seconds=86400,
                 max_seconds=3600):
        if (type(offline) is not bool or type(ttl_seconds) not in (int, float) or not math.isfinite(ttl_seconds)
                or ttl_seconds <= 0 or type(max_seconds) not in (int, float)
                or not math.isfinite(max_seconds) or max_seconds <= 0):
            raise ValueError("invalid cache or download deadline")
        self.cache_dir = Path(cache_dir)
        self.session = session if session is not None else requests.Session()
        self.offline, self.ttl_seconds, self.max_seconds = offline, ttl_seconds, max_seconds

    def _get(self, name, url, limit):
        manifest = self.cache_dir / (name + ".json")
        now = datetime.now(timezone.utc)
        if manifest.exists():
            try:
                record = json.loads(manifest.read_text(encoding="utf-8"))
                if (type(record) is not dict or set(record) !=
                        {"sha256", "size", "url", "resolved_url", "received_at", "ttl_seconds"}
                        or record["url"] != url or type(record["size"]) is not int
                        or not 0 < record["size"] <= limit
                        or type(record["ttl_seconds"]) is not type(self.ttl_seconds)
                        or record["ttl_seconds"] != self.ttl_seconds
                        or not isinstance(record["sha256"], str)
                        or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])):
                    raise ValueError("invalid manifest")
                if type(record["resolved_url"]) is not str:
                    raise ValueError("invalid resolved url")
                if record["resolved_url"] != url:
                    try:
                        self._validate_redirect(url, record["resolved_url"])
                    except ExtractError as exc:
                        raise ValueError("invalid resolved url") from exc
                if type(record["received_at"]) is not str:
                    raise ValueError("invalid received_at")
                received = datetime.fromisoformat(record["received_at"])
                if received.utcoffset() is None or received > now:
                    raise ValueError("invalid received_at")
                path = self.cache_dir / (name + "." + record["sha256"])
                digest = hashlib.sha256()
                size = 0
                with path.open("rb") as file:
                    for chunk in iter(lambda: file.read(65536), b""):
                        size += len(chunk)
                        if size > limit:
                            raise ValueError("oversized cache")
                        digest.update(chunk)
                if size != record["size"] or digest.hexdigest() != record["sha256"]:
                    raise ValueError("hash mismatch")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ExtractError("invalid_cache") from exc
            if now - received < timedelta(seconds=self.ttl_seconds):
                return path, record, True
            if self.offline:
                raise ExtractError("cache_expired")
        elif self.offline:
            raise ExtractError("cache_miss")

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        pending = None
        start = time.monotonic()
        try:
            response = self.session.get(url, headers={"User-Agent": USER_AGENT}, stream=True,
                                        timeout=(10, 45), verify=True, allow_redirects=False)
            resolved = url
            if response.status_code == 307 and url == URL:
                try:
                    resolved = response.headers.get("Location")
                    self._validate_redirect(url, resolved)
                finally:
                    response.close()
                response = self.session.get(resolved, headers={"User-Agent": USER_AGENT}, stream=True,
                                            timeout=(10, 45), verify=True, allow_redirects=False)
            with response:
                if response.status_code != 200:
                    raise ExtractError("http_error", {"http_status": response.status_code})
                try:
                    raw_length = response.headers.get("Content-Length")
                    length = int(raw_length) if raw_length is not None else None
                    if length is not None and (length < 1 or length > limit):
                        raise ValueError("invalid length")
                except (TypeError, ValueError) as exc:
                    raise ExtractError("invalid_content_length") from exc
                digest, size = hashlib.sha256(), 0
                with tempfile.NamedTemporaryFile(dir=self.cache_dir, prefix=".pending-", delete=False) as file:
                    pending = Path(file.name)
                    for chunk in response.iter_content(chunk_size=65536):
                        if time.monotonic() - start > self.max_seconds:
                            raise ExtractError("download_timeout")
                        size += len(chunk)
                        if size > limit:
                            raise ExtractError("response_too_large")
                        file.write(chunk)
                        digest.update(chunk)
                    file.flush()
                    os.fsync(file.fileno())
                if not size or length is not None and size != length:
                    raise ExtractError("truncated_response")
            record = {"sha256": digest.hexdigest(), "size": size, "url": url, "resolved_url": resolved,
                      "received_at": datetime.now(timezone.utc).isoformat(), "ttl_seconds": self.ttl_seconds}
            # Publish immutable content first; failed manifest publication leaves the old
            # pointer and its bytes intact. Orphaned new blobs are harmless.
            path = self.cache_dir / (name + "." + record["sha256"])
            try:
                os.link(pending, path)
            except FileExistsError:
                existing = hashlib.sha256()
                size = 0
                with path.open("rb") as file:
                    for chunk in iter(lambda: file.read(65536), b""):
                        size += len(chunk)
                        if size > limit:
                            raise ExtractError("invalid_cache")
                        existing.update(chunk)
                if size != record["size"] or existing.hexdigest() != record["sha256"]:
                    raise ExtractError("invalid_cache")
            pending.unlink()
            pending = None
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.cache_dir,
                                             prefix=".pending-", delete=False) as file:
                pending = Path(file.name)
                json.dump(record, file)
                file.flush()
                os.fsync(file.fileno())
            os.replace(pending, manifest)
            pending = None
            return path, record, False
        except requests.exceptions.SSLError as exc:
            raise ExtractError("tls_error") from exc
        except requests.exceptions.Timeout as exc:
            raise ExtractError("timeout") from exc
        except requests.RequestException as exc:
            raise ExtractError("fetch_failed", {"error_class": type(exc).__name__}) from exc
        finally:
            if pending is not None:
                pending.unlink(missing_ok=True)

    def collect(self, area):
        bbox = collection_bbox(area)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        lock = self.cache_dir / "collect.lock"
        token = uuid.uuid4().hex
        try:
            file = lock.open("x", encoding="utf-8")
        except FileExistsError as exc:
            raise ExtractError("cache_busy") from exc
        try:
            with file:
                file.write(token)
                file.flush()
                os.fsync(file.fileno())
            poly_path, poly_record, _ = self._get("coverage.poly", POLY_URL, MAX_POLY)
            from shapely.geometry import box
            south, west, north, east = bbox
            if not coverage_polygon(poly_path.read_bytes()).covers(box(west, south, east, north)):
                raise ExtractError("outside_extract_coverage")
            path, record, cached = self._get("northwestern-fed-district.osm.pbf", URL, MAX_PBF)
            return path, record, cached, bbox, poly_record
        finally:
            # Never remove a lock another process has replaced since our acquisition.
            try:
                if lock.read_text(encoding="utf-8") == token:
                    lock.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _validate_redirect(requested, resolved):
        try:
            parts = urlsplit(resolved)
            if (requested != URL or parts.scheme != "https" or parts.netloc != "download.geofabrik.de"
                    or parts.query or parts.fragment or not re.fullmatch(
                        r"/russia/northwestern-fed-district-\d{6}\.osm\.pbf", parts.path)):
                raise ValueError("unsafe redirect")
        except (TypeError, ValueError) as exc:
            raise ExtractError("unsafe_redirect") from exc
