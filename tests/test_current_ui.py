"""Current-stage UI on synthetic observations, without source/network access."""

from copy import deepcopy
from pathlib import Path

import pytest

st = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

from src.presentation import results


APP = Path(__file__).resolve().parents[1] / "archive_viewer.py"


@pytest.fixture
def ui_data(monkeypatch, tmp_path):
    st.cache_data.clear()
    monkeypatch.setenv("TOURISM_PRESENTATION_STORE", str(tmp_path))
    area = {"area_id": "synthetic", "name": "Тестовая территория",
            "request_geometry": {"type": "Polygon", "coordinates": [
                [[30, 59], [30.01, 59], [30.01, 59.01], [30, 59.01], [30, 59]]]},
            "analysis_geometry": None}
    run = {"run_id": "synthetic-run", "area_id": "synthetic", "status": "partial",
           "started_at": "2026-10-24T12:00:00+00:00", "limitations": ["Synthetic inputs only"]}
    view = {"run": run,
            "collection": {"study_area": area, "surrounding_m": 5000,
                           "statistics_discovery": {"status": "excluded", "reason": "tls_error"}},
            "enrichment": {"sources": {}},
            "poi": {"type": "FeatureCollection", "features": [
                {"type": "Feature", "id": "node/1", "geometry": {"type": "Point", "coordinates": [30.005, 59.005]},
                 "properties": {"tags": {"name": "Тестовый музей"}, "categories": ["tourism"],
                                "location_precision": "exact_source", "public_access": "unknown"}}]},
            "network": {"type": "FeatureCollection", "features": []},
            "matching": {"algorithm_version": "synthetic-v4", "identifier_links": [],
                         "review_candidates": [{"poi_id": "node/1", "source_record_id": "registry/1",
                                                "status": "name_only_unlocated_candidate",
                                                "evidence": {"osm_name": "Тестовый музей", "official_name": "Музей",
                                                             "distance_m": None}}],
                         "excluded_review_candidates": [], "summary": {"review_candidate_count": 1}},
            "sources": [{"source_id": "geofabrik_extract", "url": "https://example.test/source",
                         "retrieved_at": "2026-10-24T12:00:00+00:00", "completeness": "partial",
                         "parser_version": "synthetic", "content_hash": "a" * 64, "snapshot_id": "s1"}]}
    catalog = {"runs": [{**run, "name": area["name"], "query": "Synthetic",
                         "stage": "official_registry_enrichment"}], "errors": []}
    monkeypatch.setattr(results, "list_runs", lambda _: deepcopy(catalog))
    monkeypatch.setattr(results, "load_run_view", lambda *args: deepcopy(view))
    yield view, catalog
    st.cache_data.clear()


def test_empty_archive_is_not_synthetic_success(ui_data):
    _, catalog = ui_data
    catalog["runs"].clear()
    app = AppTest.from_file(str(APP)).run(timeout=20)
    assert not app.exception
    assert "нет сохранённых результатов" in app.warning[0].value
    assert not app.metric


def test_bad_run_does_not_display_map_or_metrics(ui_data, monkeypatch):
    def fail(*args):
        raise ValueError("Synthetic corrupt archive")

    monkeypatch.setattr(results, "load_run_view", fail)
    app = AppTest.from_file(str(APP)).run(timeout=20)
    assert not app.exception
    assert "не прошёл проверку" in app.error[0].value
    assert not app.metric


def test_overview_shows_observations_and_unavailable_results(ui_data):
    app = AppTest.from_file(str(APP)).run(timeout=20)
    assert not app.exception
    assert app.metric[0].label == "Наблюдения POI"
    assert app.metric[0].value == "1"
    assert app.metric[1].value == "0"
    assert "не свежий сбор" in app.info[0].value
    assert "рекомендации ещё не рассчитаны" in app.info[0].value
    assert len(app.get("download_button")) == 2


@pytest.mark.parametrize("section", ["Карта", "Сопоставление E04", "Источники", "План разработки"])
def test_sections_render_without_network_or_project_data(ui_data, section):
    app = AppTest.from_file(str(APP)).run(timeout=20)
    app.segmented_control(key="presentation_section").set_value(section).run(timeout=20)
    assert not app.exception
    if section == "Карта":
        assert app.multiselect[0].value == ["Туризм"]
        app.selectbox(key="poi_focus").select("node/1").run(timeout=20)
        assert not app.exception
        assert any("Тестовый музей" in element.value for element in app.text)
        app.text_input(key="poi_query").set_value("Нет такого объекта").run(timeout=20)
        assert not app.exception
        assert any("На карте: 0" in element.value for element in app.caption)
    elif section == "Сопоставление E04":
        assert "рекомендательные" in app.info[-1].value
        assert any("не ноль метров" in element.value for element in app.caption)
    elif section == "Источники":
        assert any("tls_error" in element.value for element in app.text)


def test_missing_enrichment_is_unknown_not_zero(ui_data):
    view, _ = ui_data
    view["matching"] = None
    app = AppTest.from_file(str(APP)).run(timeout=20)
    assert not app.exception
    assert app.metric[2].value == "не подключено"
    app.segmented_control(key="presentation_section").set_value("Сопоставление E04").run(timeout=20)
    assert not app.exception
    assert "не ноль объектов наследия" in app.warning[0].value


def test_archive_is_reverified_on_every_rerun(ui_data, monkeypatch):
    app = AppTest.from_file(str(APP)).run(timeout=20)
    assert not app.exception and app.metric

    def fail(*args):
        raise ValueError("Synthetic archive changed after first render")

    monkeypatch.setattr(results, "load_run_view", fail)
    app.segmented_control(key="presentation_section").set_value("Карта").run(timeout=20)
    assert not app.exception
    assert "не прошёл проверку" in app.error[0].value
    assert not app.metric
