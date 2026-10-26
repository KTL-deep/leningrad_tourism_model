"""Collect partial official registries for a stored territory run; no input files required."""

import argparse
from pathlib import Path
import sys
from uuid import uuid4

from scripts.collect_boundary import ROOT
from scripts.collect_territory import _journal
from src.etl.adapters.lenobl_oopt import LenoblOoptAdapter
from src.etl.adapters.mkrf_registry import MkrfRegistryAdapter
from src.etl.http_session import BoundedSession
from src.pipeline.registry_enrichment import enrich_territory_run
from src.storage import SnapshotStore


def main(argv=None, *, heritage_adapter=None, oopt_adapter=None, session=None):
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--run-id", required=True, help="Existing collect_territory RunManifest ID")
    cli.add_argument("--store", type=Path, default=ROOT / "data" / "analysis")
    cli.add_argument("--offline", action="store_true")
    args = cli.parse_args(argv)
    attempt = uuid4().hex
    log = args.store / "logs" / f"registry-enrichment-{attempt}.json"
    _journal(log, {"attempt_id": attempt, "status": "running", "source_run_id": args.run_id})
    transport = BoundedSession(session=session, cache_dir=args.store / "cache" / "http_session",
                               journal_path=args.store / "logs" / "http-requests.jsonl", offline=args.offline)
    try:
        store = SnapshotStore(args.store)
        heritage = heritage_adapter if heritage_adapter is not None else MkrfRegistryAdapter(
            store, args.store / "cache" / "mkrf_registry", session=transport, offline=args.offline)
        protected = oopt_adapter if oopt_adapter is not None else LenoblOoptAdapter(
            store, args.store / "cache" / "lenobl_oopt", session=transport, offline=args.offline)
        result = enrich_territory_run(store, args.run_id, heritage_adapter=heritage, oopt_adapter=protected)
    except (ValueError, OSError, RuntimeError) as exc:
        result = {"stage": "official_registry_enrichment", "status": "failed",
                  "source_run_id": args.run_id, "analysis_performed": False,
                  "error": getattr(exc, "code", type(exc).__name__), "error_type": type(exc).__name__}
    finally:
        transport.close()
    result["attempt_id"] = attempt
    _journal(log, result)
    import json
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["status"] == "failed" else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
