"""Assess available inputs, not tourism effects or permission for interventions."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid4

from src.data_models import RunManifest
from src.etl.source_policy import (
    DatasetCandidate,
    DatasetDecision,
    assess_candidate,
    assess_capabilities,
)
from src.storage import SnapshotStore


@dataclass(frozen=True)
class InputAssessment:
    manifest: RunManifest
    decisions: tuple[DatasetDecision, ...]

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "stage": "input_assessment",
            "manifest": self.manifest.to_dict(),
            "datasets": [decision.to_dict() for decision in self.decisions],
            "input_readiness": assess_capabilities(list(self.decisions), area_id=self.manifest.area_id),
            "computed_tourism_results": [],
        }


def prepare_inputs(
    *,
    area_id: str,
    candidates: list[DatasetCandidate],
    store: SnapshotStore,
    code_version: str,
    configuration: dict,
    run_id: str | None = None,
    started_at: datetime | None = None,
    supporting_snapshot_ids: tuple[str, ...] = (),
) -> InputAssessment:
    """Bind adapter attestations to stored bytes and record an immutable run.

    This is the first E02 integration, not automatic discovery or a geometry,
    statistical, legal, or review-content validator. Adapters must perform those
    checks before supplying attestations. Missing optional data do not disable
    independent input sets; missing essential inputs cannot produce a map.
    """
    config_bytes = json.dumps(
        configuration, sort_keys=True, ensure_ascii=False, allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    decisions = tuple(assess_candidate(candidate, area_id) for candidate in candidates)
    # A single adapter can contribute several dataset kinds from one snapshot.
    # Repeated identical source/kind attempts are ambiguous and must be explicit
    # upstream rather than silently overwrite one another here.
    keys = [(decision.source_id, decision.dataset_kind) for decision in decisions]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate source/dataset assessment")

    included_snapshots = []
    for decision in decisions:
        if not decision.included:
            continue
        snapshot = store.get_snapshot(decision.snapshot_id)
        if snapshot.area_id != area_id or snapshot.source_id != decision.source_id:
            raise ValueError("Included snapshot provenance mismatch")
        if snapshot.completeness != "complete":
            raise ValueError("Included snapshot is not complete")
        # Verify bytes, not just existence of a metadata filename.
        store.verify_content(snapshot.snapshot_id)
        included_snapshots.append(snapshot.snapshot_id)

    if not isinstance(supporting_snapshot_ids, tuple) or len(set(supporting_snapshot_ids)) != len(supporting_snapshot_ids):
        raise ValueError("Supporting snapshot references must be a unique tuple")
    for snapshot_id in supporting_snapshot_ids:
        snapshot = store.get_snapshot(snapshot_id)
        if snapshot.area_id != area_id:
            raise ValueError("Supporting snapshot area mismatch")
        store.verify_content(snapshot_id)
        # Supporting observations/artifacts do not enable a dataset capability.
        included_snapshots.append(snapshot_id)

    capabilities = assess_capabilities(list(decisions), area_id=area_id)
    if not capabilities["framework_map"]["available"]:
        status = "failed"
    elif any(not decision.included for decision in decisions) or any(
        not capability["available"] for capability in capabilities.values()
    ):
        status = "partial"
    else:
        status = "succeeded"

    limitations = [
        "Input assessment only; no tourism effects or legal permission computed."
    ]
    included_kinds = {decision.dataset_kind for decision in decisions if decision.included}
    limitations.extend(
        f"{kind}: no eligible optional dataset; not substituted with zero."
        for kind in ("official_statistics", "reviews") if kind not in included_kinds
    )
    limitations.extend(
        f"{decision.source_id}/{decision.dataset_kind}: {','.join(decision.reasons)}"
        for decision in decisions if not decision.included
    )
    limitations.extend(
        f"{name}: missing {','.join(capability['missing'])}"
        for name, capability in capabilities.items() if not capability["available"]
    )
    # Persist the decision trail as an explicitly DERIVED artifact, not a
    # source response or observed statistic. The run binds this artifact and
    # every included source snapshot; a later reader can reproduce exclusions.
    artifact = {
        "schema_version": 1,
        "artifact_kind": "derived_input_assessment",
        "algorithm_version": "input-assessment-v1",
        "area_id": area_id,
        "code_version": code_version,
        "configuration": configuration,
        "adapter_attestations": [asdict(candidate) for candidate in candidates],
        "datasets": [decision.to_dict() for decision in decisions],
        "input_readiness": capabilities,
        "computed_tourism_results": [],
        "supporting_snapshot_ids": list(supporting_snapshot_ids),
    }
    assessment_snapshot = store.save_snapshot(
        source_id="input_assessment",
        area_id=area_id,
        url="urn:tourism:derived:input-assessment:v1",
        content=json.dumps(
            artifact, sort_keys=True, ensure_ascii=False, allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8"),
        parser_version="input-assessment-v1",
        media_type="application/vnd.tourism.input-assessment+json",
        completeness="complete",
        retrieved_at=started_at,
    )
    manifest = RunManifest(
        run_id=run_id if run_id is not None else uuid4().hex,
        area_id=area_id,
        code_version=code_version,
        config_hash=hashlib.sha256(config_bytes).hexdigest(),
        started_at=started_at if started_at is not None else datetime.now(timezone.utc),
        snapshot_ids=tuple(sorted({*included_snapshots, assessment_snapshot.snapshot_id})),
        evidence_ids=(),
        status=status,
        limitations=tuple(limitations),
    )
    store.save_run(manifest)
    return InputAssessment(manifest, decisions)
