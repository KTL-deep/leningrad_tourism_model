"""Product navigation, real operation wiring and honest unimplemented sections."""

from copy import deepcopy
from pathlib import Path

import pytest

st = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

from src.presentation import operations, product_ui


APP = Path(__file__).resolve().parents[1] / "current_app.py"


@pytest.fixture
def product(monkeypatch, tmp_path):
    st.cache_data.clear()
    monkeypatch.setenv("TOURISM_PRODUCT_STORE", str(tmp_path))
    area = {"area_id": "area", "name": "Тестовый город, область",
            "request_geometry": {"type": "Polygon", "coordinates": [
                [[30, 59], [30.01, 59], [30.01, 59.01], [30, 59.01], [30, 59]]]},
            "analysis_geometry": None}
    run = {"run_id": "source-run", "area_id": "area", "status": "partial",
           "started_at": "2026-10-24T12:00:00+00:00", "limitations": []}
    view = {"run": run, "collection": {"study_area": area, "surrounding_m": 5000,
             "statistics_discovery": {"status": "excluded", "reason": "tls_error"}},
            "poi": {"type": "FeatureCollection", "features": [{"type": "Feature", "id": "node/1",
                    "geometry": {"type": "Point", "coordinates": [30.005, 59.005]},
                    "properties": {"tags": {"name": "Музей"}, "categories": ["tourism"]}}]},
            "network": {"type": "FeatureCollection", "features": []}, "enrichment": None,
            "matching": None, "sources": []}
    catalog = {"runs": [{**run, "name": area["name"], "query": "Тестовый город",
                         "stage": "territory_collection"}], "errors": []}
    monkeypatch.setattr(product_ui, "list_runs", lambda _: deepcopy(catalog))
    monkeypatch.setattr(product_ui, "load_run_view", lambda *args: deepcopy(view))
    yield view, catalog
    st.cache_data.clear()


def button(app, label):
    return next(item for item in app.button if item.label == label)


def open_project(app):
    button(app, "Открыть проект").click().run(timeout=20)
    assert not app.exception
    return app


def test_start_is_product_workflow_not_development_report(product):
    app = AppTest.from_file(str(APP)).run(timeout=20)
    assert not app.exception
    assert app.title[0].value == "Территория"
    assert button(app, "Собрать и открыть проект")
    assert button(app, "Открыть проект")
    assert not app.metric


def test_open_and_switch_to_implemented_framework(product):
    app = open_project(AppTest.from_file(str(APP)).run(timeout=20))
    assert app.session_state.product_run_id == "source-run"
    assert app.metric[0].value == "1"
    app.switch_page("app_pages/framework.py").run(timeout=20)
    assert not app.exception
    assert app.title[0].value == "Каркас территории"
    app.text_input(key="product_poi_query").set_value("Не найдено").run(timeout=20)
    assert not app.exception
    assert any("Показано 0" in element.value for element in app.caption)


@pytest.mark.parametrize("page", ["restrictions", "accessibility", "services", "trips", "interventions", "scenarios"])
def test_future_sections_are_explicit_disabled_placeholders(product, page):
    app = AppTest.from_file(str(APP)).run(timeout=20)
    app.switch_page(f"app_pages/{page}.py").run(timeout=20)
    assert not app.exception
    assert app.warning[0].value == "В разработке"
    assert app.button and all(item.disabled for item in app.button)
    assert not app.metric and not app.dataframe


def test_data_pages_require_project_without_fabricating_results(product):
    app = AppTest.from_file(str(APP)).run(timeout=20)
    app.switch_page("app_pages/framework.py").run(timeout=20)
    assert not app.exception
    assert "Сначала выберите территорию" in app.info[0].value


def test_collection_button_calls_backend_and_activates_verified_run(product, monkeypatch):
    calls = []

    def collect(query, store, **kwargs):
        calls.append((query, store, kwargs))
        return {"ok": True, "result": {"run_manifest_id": "new-run", "status": "partial"}, "error": None}

    monkeypatch.setattr(operations, "collect_territory", collect)
    app = AppTest.from_file(str(APP)).run(timeout=20)
    app.text_input(key="product_query").set_value("Гатчина, Россия")
    app.checkbox(key="product_offline").check()
    button(app, "Собрать и открыть проект").click().run(timeout=20)
    assert not app.exception
    assert len(calls) == 1 and calls[0][0] == "Гатчина, Россия" and calls[0][2] == {"offline": True}
    assert app.session_state.product_run_id == "new-run"
    assert any("собраны" in element.value for element in app.success)


def test_failed_collection_preserves_open_project(product, monkeypatch):
    monkeypatch.setattr(operations, "collect_territory", lambda *args, **kwargs:
                        {"ok": False, "error": "cache_expired", "result": None})
    app = open_project(AppTest.from_file(str(APP)).run(timeout=20))
    app.text_input(key="product_query").set_value("Другая территория")
    button(app, "Собрать и открыть проект").click().run(timeout=20)
    assert not app.exception
    assert app.session_state.product_run_id == "source-run"
    assert any("cache_expired" in element.value for element in app.text)


def test_enrichment_uses_collection_parent_not_child(product, monkeypatch):
    view, _ = product
    view["enrichment"] = {"source_run_id": "collection-parent", "sources": {}}
    calls = []

    def enrich(rid, store, **kwargs):
        calls.append(rid)
        return {"ok": True, "result": {"run_manifest_id": "enriched-run"}, "error": None}

    monkeypatch.setattr(operations, "enrich_territory", enrich)
    app = open_project(AppTest.from_file(str(APP)).run(timeout=20))
    app.switch_page("app_pages/sources.py").run(timeout=20)
    button(app, "Подключить / обновить каталоги").click().run(timeout=20)
    assert not app.exception
    assert calls == ["collection-parent"]
    assert app.session_state.product_run_id == "enriched-run"


def test_changed_store_clears_active_project(product):
    app = open_project(AppTest.from_file(str(APP)).run(timeout=20))
    app.text_input(key="product_store").set_value("different-store").run(timeout=20)
    assert not app.exception
    assert app.session_state.product_run_id is None
