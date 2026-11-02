"""Bounded product-UI adapters to the existing E03/E04 command-line entry points."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from src.data_models.contracts import valid_id
from src.etl.heritage_validation import strict_json
from src.presentation.results import load_run_view


ROOT = Path(__file__).resolve().parents[2]
OUTPUT_LIMIT = 4 * 1024 * 1024


class _Busy(Exception):
    pass


@contextmanager
def _action_lock(store_path: Path):
    lock_path = store_path / "logs" / "product-ui" / "action.lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise _Busy("Another product action is running for this store") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as lock:
            identity = os.fstat(lock.fileno())
            lock.write(f"{os.getpid()}\n")
            lock.flush()
            yield
    finally:
        # Only release the lock acquired here; never remove a replacement/foreign lock.
        try:
            current = lock_path.stat()
        except FileNotFoundError:
            pass
        else:
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                lock_path.unlink()


def _json_result(path: Path) -> dict:
    with path.open("rb") as stream:
        data = stream.read(OUTPUT_LIMIT + 1)
    if not data:
        raise ValueError("No CLI JSON output")
    if len(data) > OUTPUT_LIMIT:
        raise ValueError("CLI JSON output exceeds 4 MiB")
    result = strict_json(data)
    if not isinstance(result, dict):
        raise ValueError("CLI JSON output must be an object")
    return result


def _run(module: str, args: list[str], store_path: Path, timeout: int, stage: str,
         source_run_id: str | None = None, offline: bool = False) -> dict:
    store = Path(store_path).resolve()
    logs = store / "logs" / "product-ui"
    stdout_path = stderr_path = None
    response = {"ok": False, "result": None, "error": None,
                "stdout_log": "", "stderr_log": "", "returncode": None}
    try:
        logs.mkdir(parents=True, exist_ok=True)
        token = uuid4().hex
        stdout_path, stderr_path = logs / f"{token}.stdout.log", logs / f"{token}.stderr.log"
        response["stdout_log"], response["stderr_log"] = str(stdout_path), str(stderr_path)
        with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
            with _action_lock(store):
                completed = subprocess.run(
                    [sys.executable, "-X", "utf8", "-m", module, *args, "--store", str(store)]
                    + (["--offline"] if offline else []),
                    cwd=ROOT, shell=False, stdout=stdout, stderr=stderr, timeout=timeout,
                    check=False,
                )
                response["returncode"] = completed.returncode
                result = _json_result(stdout_path)
                response["result"] = result
                if completed.returncode != 0:
                    raise ValueError(f"CLI exited with status {completed.returncode}")
                run_id = result.get("run_manifest_id")
                if not isinstance(run_id, str):
                    raise ValueError("CLI did not return a run manifest ID")
                valid_id(run_id)
                if stage == "territory_collection":
                    if (result.get("stage") != stage or
                            result.get("collection_status") != "succeeded" or
                            result.get("required_spatial_inputs_collected") is not True):
                        raise ValueError("Collection did not collect required spatial inputs")
                elif (result.get("stage") != stage or result.get("status") not in ("partial", "succeeded")
                      or result.get("source_run_id") != source_run_id):
                    raise ValueError("Enrichment did not return a valid partial/succeeded run")
                view = load_run_view(store, run_id)
                if not isinstance(view, dict) or not isinstance(view.get("run"), dict) or view["run"].get("run_id") != run_id:
                    raise ValueError("Returned run could not be verified")
                if stage == "territory_collection" and view.get("enrichment") is not None:
                    raise ValueError("Returned run is not a collection run")
                if stage == "official_registry_enrichment" and (
                        not isinstance(view.get("enrichment"), dict)
                        or view["enrichment"].get("source_run_id") != source_run_id):
                    raise ValueError("Returned enrichment has the wrong source run")
                response["ok"] = True
    except (_Busy, OSError, ValueError, UnicodeError, json.JSONDecodeError,
            subprocess.TimeoutExpired, TypeError, KeyError) as exc:
        response["error"] = str(exc) or type(exc).__name__
        if stderr_path is not None:
            try:
                with stderr_path.open("a", encoding="utf-8") as log:
                    log.write(f"\nProduct UI: {type(exc).__name__}: {response['error']}\n")
            except OSError:
                pass
    return response


def collect_territory(territory: str, store_path: Path, *, offline: bool = False,
                      timeout: int = 1800) -> dict:
    """Run E03 for a user-supplied territory; never infer success from exit code alone."""
    if (not isinstance(territory, str) or not territory.strip() or len(territory) > 1000
            or "\x00" in territory or territory.startswith("-")):
        return {"ok": False, "result": None, "error": "Invalid territory",
                "stdout_log": "", "stderr_log": "", "returncode": None}
    return _run("scripts.collect_territory", [territory], store_path, timeout,
                "territory_collection", offline=offline)


def enrich_territory(source_run_id: str, store_path: Path, *, offline: bool = False,
                     timeout: int = 1800) -> dict:
    """Enrich only a verified E03 parent; E04 availability remains explicit."""
    try:
        valid_id(source_run_id)
        view = load_run_view(Path(store_path).resolve(), source_run_id)
        if (not isinstance(view, dict) or not isinstance(view.get("run"), dict)
                or view["run"].get("run_id") != source_run_id
                or view.get("enrichment") is not None):
            raise ValueError("E04 requires a verified E03 collection run")
    except (ValueError, OSError, TypeError, KeyError) as exc:
        return {"ok": False, "result": None, "error": f"E04 unavailable: {exc}",
                "stdout_log": "", "stderr_log": "", "returncode": None}
    return _run("scripts.enrich_territory", ["--run-id", source_run_id], store_path,
                timeout, "official_registry_enrichment", source_run_id, offline)
