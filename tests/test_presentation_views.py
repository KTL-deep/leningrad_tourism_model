"""Offline, synthetic presentation-only checks."""

from copy import deepcopy

import pytest

from src.presentation.views import (CATEGORY_LABELS, map_spec, matching_rows, poi_rows,
                                    report_markdown, summary_report)


POLYGON = {"type": "Polygon", "coordinates": [[[30, 59], [31, 59], [31, 60],
                                                    [30, 60], [30, 59]]]}


def _poi(identifier="node/1", coords=(30.5, 59.5)):
    return {"type": "Feature", "id": identifier,
            "geometry": {"type": "Point", "coordinates": list(coords)},
            "properties": {"tags": {"name": "<i>Замок</i>", "access": "yes"},
                           "categories": ["tourism", "food", "other"],
                           "location_precision": "exact_source", "public_access": None,
                           "capacity": None}}


def _view():
    return {"run": {"run_id": "run1", "area_id": "area1", "status": "partial",
                    "started_at": "2026-11-02T12:00:00+00:00", "limitations": ["<b>Неполный</b>"]},
            "collection": {"study_area": {"name": "Тест <script>alert(1)</script>",
                                          "request_geometry": POLYGON,
                                          "analysis_geometry": POLYGON},
                           "statistics_discovery": {"status": "excluded"}},
            "enrichment": None, "poi": {"features": [_poi()]},
            "network": {"features": [{"type": "Feature", "id": "way/2",
                                      "geometry": {"type": "LineString",
                                                   "coordinates": [[30, 59], [31, 60]]},
                                      "properties": {}}]},
            "matching": None,
            "sources": [{"source_id": "osm", "url": "https://example.org/osm?a=1&b=2",
                         "retrieved_at": "2026-11-01T00:00:00+00:00", "content_hash": "ab" * 32,
                         "completeness": "partial", "snapshot_id": "snap1"}]}


def test_poi_rows_only_valid_points_without_mutation():
    fc = {"features": [_poi(), _poi("node/2", (float("nan"), 59)),
                       {**_poi("node/3"), "geometry": {"type": "LineString", "coordinates": []}},
                       _poi("node/4", (181, 59))]}
    before = deepcopy(fc)
    rows = poi_rows(fc)
    assert fc == before
    assert len(rows) == 1
    assert rows[0] == {"id": "node/1", "name": "<i>Замок</i>",
                       "categories": "Туризм, Питание, other", "longitude": 30.5,
                       "latitude": 59.5, "precision": "exact_source", "access": "неизвестно",
                       "capacity": "неизвестно", "tags": {"name": "<i>Замок</i>", "access": "yes"}}
    rows[0]["tags"]["name"] = "изменено"
    assert fc["features"][0]["properties"]["tags"]["name"] == "<i>Замок</i>"
    assert {"tourism", "accommodation", "food", "transport", "leisure"} <= set(CATEGORY_LABELS)
    assert poi_rows({"features": [{**_poi(), "properties": {"tags": {}, "categories": []}}]})[0]["name"] == "node/1"
    assert poi_rows({"features": [{**_poi(), "properties": {"public_access": "unknown"}}]})[0]["access"] == "неизвестно"
    assert poi_rows({"features": [{**_poi(), "properties": {}}]})[0]["categories"] == "Без категории"


def test_matching_rows_preserve_uncertainty():
    assert matching_rows(None) == []
    evidence = {"osm_name": "OSM", "official_name": "Реестр", "distance_m": None,
                "warnings": ["нет точки", "не вход"]}
    report = {"identifier_links": [{"poi_id": "node/1", "source_record_id": "r1",
                                    "status": "ambiguous_identifier", "evidence": evidence}],
              "review_candidates": [{"poi_id": "node/2", "source_record_id": "r2",
                                     "status": "name_only_unlocated_candidate", "evidence": evidence}],
              "excluded_review_candidates": [{"poi_id": "node/3", "source_record_id": "r3",
                                              "status": "excluded_name_candidate", "reason": "no_role",
                                              "evidence": evidence}]}
    before = deepcopy(report)
    rows = matching_rows(report)
    assert report == before
    assert [r["channel"] for r in rows] == ["ID-связь", "Кандидат", "Исключён из очереди"]
    assert all(r["distance_m"] is None and r["warnings"] == "нет точки; не вход" for r in rows)
    assert rows[0]["status"] == "ambiguous_identifier"
    assert rows[2]["reason"] == "no_role"


def test_map_spec_inline_layers_all_roads_and_altair_validation():
    alt = pytest.importorskip("altair")
    view = _view()
    rows = poi_rows(view["poi"])
    before = deepcopy(view)
    spec = map_spec(view["collection"], view["network"], rows, selected_id="node/1")
    assert view == before
    assert spec["height"] == 560 and spec["projection"] == {"type": "mercator"}
    assert len(spec["layer"]) == 5
    assert spec["layer"][2]["data"]["values"] == view["network"]["features"]
    assert spec["layer"][4]["data"]["values"] == rows
    assert '"url"' not in __import__("json").dumps(spec)
    alt.LayerChart.from_dict(spec).to_dict()
    assert len(map_spec(view["collection"], view["network"], rows, False)["layer"]) == 3


def test_summary_and_markdown_provenance_without_raw_html():
    view = _view()
    before = deepcopy(view)
    summary = summary_report(view)
    assert view == before
    assert summary["heritage"] is None and summary["statistics_discovery"] == {"status": "excluded"}
    assert summary["poi_observation_count"] == summary["road_observation_count"] == 1
    assert summary["sources"][0]["snapshot_id"] == "snap1"
    assert "<b>Неполный</b>" in summary["limitations"]
    text = report_markdown(view)
    assert "<script>" not in text and "<b>" not in text
    assert "https://example" in text and "ab" * 32 in text
    assert "ODbL" in text and "Рекомендации" in text
    view["matching"] = {"summary": {"identifier_link_count": 2}}
    view["enrichment"] = {"stage": "official_registry_enrichment"}
    assert summary_report(view)["heritage"] == {"identifier_link_count": 2}
