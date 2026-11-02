"""The real-data developer challenge set is frozen, explicit and non-independent."""

from collections import Counter
from pathlib import Path

from src.etl.heritage_validation import strict_json, validate_heritage_benchmark


def test_frozen_developer_control_has_traceable_positive_negative_and_unknown_cases():
    path = Path(__file__).resolve().parents[1] / "benchmarks/heritage_matching/e04_control_v1.json"
    benchmark = strict_json(path.read_bytes())
    validate_heritage_benchmark(benchmark)
    assert benchmark["review_basis"] == "developer_evidence_review_not_independent_ground_truth"
    assert "not held out" in benchmark["selection_protocol"].lower()
    assert Counter(case["label"] for case in benchmark["cases"]) == {
        "supported": 9, "contradicted": 7, "unresolved": 4,
    }
    assert len({case["source_run_id"] for case in benchmark["cases"]}) == 2
    cases = {case["case_id"]: case for case in benchmark["cases"]}
    assert cases["C15"]["poi_id"] == "relation/11914532"
    assert cases["C16"]["poi_id"] == "relation/9324726"
    assert cases["C15"]["label"] == cases["C16"]["label"] == "supported"
    assert cases["C19"]["label"] == cases["C20"]["label"] == "contradicted"
    assert cases["C06"]["label"] == "unresolved"  # A history board is not the column itself.
