import os

import pytest

gpd = pytest.importorskip("geopandas")

from src.etl.gis_loader import GISLoader


def test_load_dem_missing_returns_none(tmp_path):
    loader = GISLoader(data_dir=str(tmp_path))
    assert loader.load_dem("missing_dem.tif") is None


def test_load_protected_areas_missing_returns_empty(tmp_path):
    loader = GISLoader(data_dir=str(tmp_path))
    gdf = loader.load_protected_areas("missing_oopt.geojson")
    assert isinstance(gdf, gpd.GeoDataFrame)
    assert gdf.empty
    assert str(gdf.crs) in ("EPSG:4326", "epsg:4326")


def test_load_cultural_heritage_uses_local_file_when_exists(tmp_path, monkeypatch):
    # Создаём минимальный GeoJSON, чтобы не зависеть от внешних данных
    p = tmp_path / "okn.geojson"
    p.write_text(
        """{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "properties": {"name": "test"},
      "geometry": {"type": "Point", "coordinates": [30.0, 60.0]}
    }
  ]
}""",
        encoding="utf-8",
    )

    loader = GISLoader(data_dir=str(tmp_path))

    # Если файл существует, сетевой метод вызываться не должен
    monkeypatch.setattr(loader, "_fetch_okn_from_mkrf_api", lambda *args, **kwargs: pytest.fail("network fetch called"))
    gdf = loader.load_cultural_heritage("okn.geojson")

    assert not gdf.empty
    assert "geometry" in gdf.columns


def test_load_cultural_heritage_falls_back_to_fetch(tmp_path, monkeypatch):
    loader = GISLoader(data_dir=str(tmp_path))

    empty = gpd.GeoDataFrame(columns=["geometry"], geometry="geometry", crs="EPSG:4326")
    monkeypatch.setattr(loader, "_fetch_okn_from_mkrf_api", lambda *args, **kwargs: empty)

    gdf = loader.load_cultural_heritage("okn.geojson")
    assert isinstance(gdf, gpd.GeoDataFrame)
    assert gdf.empty
    assert os.path.exists(str(tmp_path))  # sanity

