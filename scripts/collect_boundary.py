"""Collect one explicitly requested territory boundary; not a full tourism analysis."""

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys

from src.etl.adapters.nominatim_boundary import BoundaryError, NominatimBoundaryAdapter
from src.etl.source_policy import DatasetCandidate
from src.pipeline.input_assessment import prepare_inputs
from src.storage import SnapshotStore


ROOT = Path(__file__).resolve().parents[1]


def code_fingerprint() -> str:
    """Fingerprint local source, including uncommitted edits, not just Git HEAD."""
    digest = hashlib.sha256()
    for path in sorted([*ROOT.joinpath("src").rglob("*.py"), *ROOT.joinpath("scripts").rglob("*.py")]):
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8") + b"\x00")
        digest.update(path.read_bytes() + b"\x00")
    return "sha256:" + digest.hexdigest()


def main(argv=None, *, adapter=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("territory", help="Explicit territory name, with region to avoid ambiguity")
    parser.add_argument("--store", type=Path, default=ROOT / "data" / "analysis")
    parser.add_argument("--offline", action="store_true", help="Use only a valid fresh cached response")
    args = parser.parse_args(argv)
    if not args.territory.strip():
        parser.error("territory must not be empty")
    store = SnapshotStore(args.store)
    try:
        client = adapter if adapter is not None else NominatimBoundaryAdapter(
            store=store, cache_dir=args.store / "cache" / "nominatim", offline=args.offline,
            endpoint=os.environ.get("NOMINATIM_ENDPOINT", "https://nominatim.openstreetmap.org/search"),
            terms_confirmed=os.environ.get("NOMINATIM_TERMS_CONFIRMED") == "1",
        )
        collected = client.collect(args.territory)
        area = collected.study_area
        snapshot = collected.snapshot
        if not isinstance(collected.observed_at, datetime) or collected.observed_at.utcoffset() is None:
            raise ValueError("Boundary observation date must be timezone-aware")
        # These attestations are for the returned OSM polygon only. They do not
        # establish legal boundaries, surrounding-network coverage, or the
        # validity period of any official statistic.
        candidate = DatasetCandidate(
            source_id=snapshot.source_id, dataset_kind="boundary", area_id=area.area_id,
            discovered_automatically=True, retrieved=True, content_validated=True,
            provenance_verified=True, coverage_verified=True, period_verified=True,
            units_verified=True, access_permitted=True, completeness="complete",
            snapshot_id=snapshot.snapshot_id,
        )
        assessment = prepare_inputs(
            area_id=area.area_id, candidates=[candidate], store=store,
            code_version=code_fingerprint(), configuration={
                "stage": "boundary_collection",
                "query": args.territory,
                "study_area": area.to_dict(),
                "boundary_observed_at": collected.observed_at.isoformat(),
                "period_basis": "source_observed_at; not legal validity or publication date",
                "coverage_basis": "complete selected polygon only; no POI or network coverage claim",
                "source_license": "ODbL",
                "source_policy": "https://operations.osmfoundation.org/policies/nominatim/",
                "from_cache": collected.from_cache,
            },
        )
        area_summary = area.to_dict()
        area_summary.pop("request_geometry", None)
        area_summary.pop("analysis_geometry", None)
        output = {
            "stage": "boundary_collection",
            "status": "partial",
            "boundary_status": "succeeded",
            "analysis_performed": False,
            "study_area": area_summary,
            "source": snapshot.to_dict(),
            "from_cache": collected.from_cache,
            "observed_at": collected.observed_at.isoformat(),
            "attribution": collected.attribution,
            "input_assessment": assessment.to_dict(),
            "limitations": [
                "OSM observation is not certification of a legal administrative boundary.",
                "Surrounding area, POI, network, statistics and reviews are not collected by this command.",
                "Readiness is not computed tourism effects or permission for interventions.",
            ],
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except BoundaryError as exc:
        print(json.dumps({
            "stage": "boundary_collection", "status": "failed", "analysis_performed": False,
            "query": args.territory, "error": exc.code,
            "details": getattr(exc, "details", None),
        }, ensure_ascii=False, indent=2))
        return 2
    except (ValueError, OSError) as exc:
        print(json.dumps({
            "stage": "boundary_collection", "status": "failed", "analysis_performed": False,
            "query": args.territory, "error": "local_state_or_configuration_error",
            "details": {"error_class": type(exc).__name__, "message": str(exc)},
        }, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
