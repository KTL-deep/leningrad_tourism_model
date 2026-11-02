"""Headless baseline labels on synthetic data, without reading project outputs."""

import json
from pathlib import Path

import pytest

st = pytest.importorskip("streamlit")
gpd = pytest.importorskip("geopandas")
streamlit_folium = pytest.importorskip("streamlit_folium")
from shapely.geometry import box
from streamlit.testing.v1 import AppTest


APP = Path(__file__).resolve().parents[1] / "app.py"


@pytest.fixture(autouse=True)
def isolated_streamlit_cache():
    st.cache_data.clear()
    yield
    st.cache_data.clear()


@pytest.mark.parametrize("missing", [True, False], ids=["missing-file", "empty-data"])
def test_baseline_warning_visible_without_data(monkeypatch, missing):
    def read_file(path):
        assert path == "data/processed/ucm_blocks_optimized.geojson"
        if missing:
            raise FileNotFoundError("Synthetic missing baseline output")
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

    monkeypatch.setattr(gpd, "read_file", read_file)
    app = AppTest.from_file(str(APP)).run(timeout=20)

    assert not app.exception
    assert "baseline" in app.title[0].value
    assert "не рекомендации администрации" in app.warning[0].value
    assert "не прогноз туристического потока" in app.warning[0].value
    assert "Ожидание данных" in app.warning[1].value
    assert not app.selectbox


def test_baseline_comparison_and_capacity_are_not_recommendations(monkeypatch):
    data = {"block_id": [1]}
    for scenario in ("Экоцентризм", "Историко-центризм", "Инфраструктурный"):
        slug = scenario.replace("-", "_")
        data[f"Target_LandUse_{scenario}"] = ["Опорные центры (Хабы)"]
        data[f"Capacity_{scenario}"] = [300.0]
        data[f"S_ik_{slug}_Опорные_центры_(Хабы)"] = [0.5]
    blocks = gpd.GeoDataFrame(data, geometry=[box(30, 59, 30.01, 59.01)], crs="EPSG:4326")

    def read_file(path):
        assert path == "data/processed/ucm_blocks_optimized.geojson"
        return blocks.copy()

    maps = []
    monkeypatch.setattr(gpd, "read_file", read_file)
    monkeypatch.setattr(streamlit_folium, "st_folium", lambda m, **kwargs: maps.append(m))
    app = AppTest.from_file(str(APP)).run(timeout=20)

    assert not app.exception
    rendered = "\n".join(element.value for element in app.markdown)
    assert "Сравнение сценариев baseline" in rendered
    assert "Максимальный балл baseline" in rendered
    assert "Условный Capacity baseline" in rendered
    assert "РЕКОМЕНДУЕМЫЙ" not in rendered
    assert "Лучший сценарий развития территорий" not in rendered
    assert "Емкость размещения:" not in rendered
    assert "не рекомендации администрации" in app.warning[0].value
    assert maps
    # Folium serializes tooltip aliases as JSON, including escaped Cyrillic.
    assert json.dumps("Условный Capacity (не число людей):") in maps[-1].get_root().render()

    app.selectbox[1].select("Условный Capacity baseline").run(timeout=20)

    assert not app.exception
    assert any("не число людей" in element.value for element in app.markdown)
    assert "не рекомендации администрации" in app.warning[0].value
