"""Bounded, sequential reachability probes; observations are not dataset validation."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlsplit

import requests


CONFIG = Path(__file__).resolve().parents[1] / "configs" / "source_probes.json"
MAX_BYTES = 1_048_576
HEADERS = {"User-Agent": "leningrad_tourism_model-source-probe/1.0 (+https://github.com/KTL-deep/leningrad_tourism_model)"}


def probe(source, session, last_request=None, *, clock=time.monotonic, sleep=time.sleep):
    """Probe one configured URL. last_request tracks start times by hostname."""
    if last_request is None:
        last_request = {}
    host = urlsplit(source["url"]).hostname
    if host in last_request:
        wait = 1 - (clock() - last_request[host])
        if wait > 0:
            sleep(wait)
    last_request[host] = clock()

    result = {key: source[key] for key in (
        "source_id", "owner", "url", "coverage", "categories", "known_conditions", "known_gaps"
    )}
    result.update({key: source.get(key) for key in (
        "access_method", "authentication", "license", "update_frequency",
        "rate_limit", "fields", "units", "adapter_version",
    )})
    result["probe_version"] = "source-probe-v1"
    result.update(checked_at=datetime.now(timezone.utc).isoformat(), final_url=None,
                  http_status=None, content_type=None, body_sha256=None,
                  captured_bytes=0, truncated=False, status="error",
                  error_class=None, error_detail=None, top_level_keys=None,
                  element_count=None)
    body = bytearray()
    try:
        with session.get(source["url"], params=source.get("params"), headers=HEADERS,
                         timeout=(10, 25), stream=True, verify=True) as response:
            result["final_url"] = response.url
            result["http_status"] = response.status_code
            result["content_type"] = response.headers.get("Content-Type")
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                remaining = MAX_BYTES - len(body)
                body.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    result["truncated"] = True
                    break
    except requests.RequestException as exc:
        result["error_class"] = type(exc).__name__
        result["error_detail"] = str(exc)
    finally:
        result["captured_bytes"] = len(body)
        result["body_sha256"] = hashlib.sha256(body).hexdigest()

    if result["error_class"]:
        return result
    text = body[:65536].decode("utf-8", errors="replace").lower()
    if result["http_status"] in (403, 429):
        result["status"] = "access_denied" if result["http_status"] == 403 else "rate_limited"
    elif ("location.reload" in text and "beget=begetok" in text or
          len(body) < 16384 and "captcha" in text and
          any(label in text for label in ("verify you are human", "access verification", "подтвердите, что вы"))):
        result["status"] = "access_challenge"
    elif not 200 <= result["http_status"] < 300:
        result["status"] = "http_error"
    else:
        result["status"] = "http_ok"
        if not result["truncated"] and "json" in (result["content_type"] or "").lower():
            try:
                parsed = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                pass
            else:
                if isinstance(parsed, dict):
                    result["top_level_keys"] = list(parsed)
                    if isinstance(parsed.get("elements"), list):
                        result["element_count"] = len(parsed["elements"])
                elif isinstance(parsed, list):
                    result["element_count"] = len(parsed)
    return result


def main(argv=None, *, session=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/audit/source_probes.json"))
    parser.add_argument("--source", action="append", dest="sources", metavar="ID",
                        help="Source ID (repeat to select multiple; default: all)")
    args = parser.parse_args(argv)
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    if config["schema_version"] != 1:
        parser.error("Unsupported source config schema")
    available = {source["source_id"]: {**config.get("defaults", {}), **source}
                 for source in config["sources"]}
    for source_id in args.sources or []:
        if source_id not in available:
            parser.error(f"Unknown source ID: {source_id}")
    selected = [available[source_id] for source_id in (args.sources or available)]
    # Refuse an existing target before any network traffic; exclusive creation also
    # protects against a concurrent writer racing this check.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        last_request = {}
        client = session if session is not None else requests.Session()
        try:
            observations = [probe(source, client, last_request) for source in selected]
            json.dump({"schema_version": 1, "probes": observations}, output, ensure_ascii=False, indent=2)
            output.write("\n")
        finally:
            if session is None:
                client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
