"""Content-addressed raw bytes and immutable provenance records."""

from dataclasses import fields, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile

from src.data_models.contracts import Evidence, RunManifest, SourceSnapshot, valid_id


def _json(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")


def _snapshot_identity(snapshot: SourceSnapshot) -> str:
    identity = [snapshot.source_id, snapshot.area_id, snapshot.url, snapshot.content_hash,
                snapshot.parser_version, snapshot.media_type, snapshot.completeness]
    return hashlib.sha256(_json({"identity": identity})).hexdigest()


def _load(path: Path, model: type) -> object:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {field.name for field in fields(model)}:
        raise ValueError(f"Invalid {model.__name__} record: {path}")
    if data["schema_version"] != 1 or type(data["schema_version"]) is not int:
        raise ValueError("Unsupported schema version")
    if model is SourceSnapshot:
        data["retrieved_at"] = datetime.fromisoformat(data["retrieved_at"])
    elif model is RunManifest:
        data["started_at"] = datetime.fromisoformat(data["started_at"])
        data["snapshot_ids"] = tuple(data["snapshot_ids"])
        data["evidence_ids"] = tuple(data["evidence_ids"])
        data["limitations"] = tuple(data["limitations"])
    return model(**data)


def _publish(path: Path, content: bytes, *, retain_existing: bool = False) -> None:
    """Link a complete temporary file into place without ever replacing an existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".pending-", delete=False) as file:
            temp = Path(file.name)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        try:
            os.link(temp, path)
        except FileExistsError:
            if not retain_existing and path.read_bytes() != content:
                raise ValueError(f"Immutable record conflict: {path}")
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def _hash_file(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(65536), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


class SnapshotStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, category: str, identifier: str, suffix: str = ".json") -> Path:
        return self.root / category / (valid_id(identifier) + suffix)

    def get_snapshot(self, snapshot_id: str) -> SourceSnapshot:
        snapshot = _load(self._path("snapshots", snapshot_id), SourceSnapshot)
        if snapshot.snapshot_id != snapshot_id or _snapshot_identity(snapshot) != snapshot_id:
            raise ValueError("Snapshot identity mismatch")
        return snapshot

    def save_snapshot(self, source_id: str, area_id: str, url: str, content: bytes,
                      parser_version: str, media_type: str, completeness: str = "unknown",
                      retrieved_at: datetime | None = None) -> SourceSnapshot:
        if not isinstance(content, bytes):
            raise TypeError("content must be bytes")
        content_hash = hashlib.sha256(content).hexdigest()
        snapshot = SourceSnapshot("pending", source_id, area_id, url, content_hash,
                                  len(content), parser_version, media_type, completeness,
                                  retrieved_at if retrieved_at is not None else datetime.now(timezone.utc))
        return self._save_snapshot(snapshot, lambda: _publish(self._path("blobs", content_hash, ".bin"), content))

    def save_snapshot_file(self, source_id: str, area_id: str, url: str, file_path: Path,
                           parser_version: str, media_type: str, completeness: str = "unknown",
                           retrieved_at: datetime | None = None, *, expected_hash: str | None = None,
                           expected_size: int | None = None) -> SourceSnapshot:
        """Copy a file into an independent immutable blob, validating the copied bytes."""
        blob_dir = self.root / "blobs"
        blob_dir.mkdir(parents=True, exist_ok=True)
        pending = None
        try:
            digest, size = hashlib.sha256(), 0
            with Path(file_path).open("rb") as source, tempfile.NamedTemporaryFile(
                    dir=blob_dir, prefix=".pending-", delete=False) as target:
                pending = Path(target.name)
                for chunk in iter(lambda: source.read(65536), b""):
                    target.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                target.flush()
                os.fsync(target.fileno())
            content_hash = digest.hexdigest()
            if (expected_hash is not None and content_hash != expected_hash or
                    expected_size is not None and size != expected_size):
                raise ValueError("Snapshot file hash/size mismatch")
            snapshot = SourceSnapshot("pending", source_id, area_id, url, content_hash,
                                      size, parser_version, media_type, completeness,
                                      retrieved_at if retrieved_at is not None else datetime.now(timezone.utc))

            def publish_blob():
                blob = self._path("blobs", content_hash, ".bin")
                try:
                    os.link(pending, blob)
                except FileExistsError:
                    if _hash_file(blob) != (content_hash, size):
                        raise ValueError(f"Immutable record conflict: {blob}")

            return self._save_snapshot(snapshot, publish_blob)
        finally:
            if pending is not None:
                pending.unlink(missing_ok=True)

    def _save_snapshot(self, snapshot: SourceSnapshot, publish_blob) -> SourceSnapshot:
        snapshot_id = _snapshot_identity(snapshot)
        snapshot = replace(snapshot, snapshot_id=snapshot_id)
        path = self._path("snapshots", snapshot_id)
        if path.exists():
            existing = self.get_snapshot(snapshot_id)
            if {k: v for k, v in existing.to_dict().items() if k != "retrieved_at"} != {
                k: v for k, v in snapshot.to_dict().items() if k != "retrieved_at"
            }:
                raise ValueError("Immutable snapshot conflict")
            self.verify_content(snapshot_id)
            return existing
        publish_blob()
        _publish(path, _json(snapshot.to_dict()), retain_existing=True)
        existing = self.get_snapshot(snapshot_id)
        if {k: v for k, v in existing.to_dict().items() if k != "retrieved_at"} != {
            k: v for k, v in snapshot.to_dict().items() if k != "retrieved_at"
        }:
            raise ValueError("Immutable snapshot conflict")
        self.verify_content(snapshot_id)
        return existing

    def verify_content(self, snapshot_id: str) -> SourceSnapshot:
        snapshot = self.get_snapshot(snapshot_id)
        if _hash_file(self._path("blobs", snapshot.content_hash, ".bin")) != (snapshot.content_hash, snapshot.byte_length):
            raise ValueError("Corrupt snapshot content")
        return snapshot

    def read_content(self, snapshot_id: str) -> bytes:
        snapshot = self.get_snapshot(snapshot_id)
        content = self._path("blobs", snapshot.content_hash, ".bin").read_bytes()
        if len(content) != snapshot.byte_length or hashlib.sha256(content).hexdigest() != snapshot.content_hash:
            raise ValueError("Corrupt snapshot content")
        return content

    def save_evidence(self, evidence: Evidence) -> Evidence:
        if not isinstance(evidence, Evidence):
            raise TypeError("Expected Evidence")
        self.verify_content(evidence.snapshot_id)
        path = self._path("evidence", evidence.evidence_id)
        _publish(path, _json(evidence.to_dict()))
        existing = _load(path, Evidence)
        if existing != evidence:
            raise ValueError("Immutable evidence conflict")
        return existing

    def get_evidence(self, evidence_id: str) -> Evidence:
        evidence = _load(self._path("evidence", evidence_id), Evidence)
        if evidence.evidence_id != evidence_id:
            raise ValueError("Evidence ID mismatch")
        self.verify_content(evidence.snapshot_id)
        return evidence

    def _validate_run_references(self, run: RunManifest) -> None:
        for snapshot_id in run.snapshot_ids:
            if self.get_snapshot(snapshot_id).area_id != run.area_id:
                raise ValueError("Run snapshot area mismatch")
            self.verify_content(snapshot_id)
        for evidence_id in run.evidence_ids:
            evidence = self.get_evidence(evidence_id)
            if evidence.snapshot_id not in run.snapshot_ids:
                raise ValueError("Run evidence reference mismatch")

    def get_run(self, run_id: str) -> RunManifest:
        run = _load(self._path("runs", run_id), RunManifest)
        if run.run_id != run_id:
            raise ValueError("Run ID mismatch")
        self._validate_run_references(run)
        return run

    def save_run(self, run: RunManifest) -> None:
        if not isinstance(run, RunManifest):
            raise TypeError("Expected RunManifest")
        self._validate_run_references(run)
        _publish(self._path("runs", run.run_id), _json(run.to_dict()))
        if self.get_run(run.run_id) != run:
            raise ValueError("Immutable run conflict")
