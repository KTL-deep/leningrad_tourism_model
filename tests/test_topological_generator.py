"""Offline synthetic tests for the installed BlocksNet unclassified cutter."""

import subprocess
import sys

import pytest

gpd = pytest.importorskip("geopandas")
pytest.importorskip("osmnx")
pytest.importorskip("blocksnet")
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box

from src.generation.blocksnet_cutting import cut_urban_blocks
from src.generation.topological_generator import TopologicalGenerator


CRS = "EPSG:32636"
X, Y = 500000, 6600000


def layer(geometries, crs=CRS):
    return gpd.GeoDataFrame(geometry=list(geometries), crs=crs)


def boundary():
    return layer([box(X, Y, X + 100, Y + 100)])


def test_cut_roads_and_empty_layers():
    road = layer([LineString([(X + 50, Y), (X + 50, Y + 100)])])
    blocks = cut_urban_blocks(boundary(), road, layer([]), None)
    assert blocks.crs == boundary().crs
    assert len(blocks) == 2
    assert sorted(blocks.area.tolist()) == pytest.approx([5000, 5000])
    uncut = cut_urban_blocks(boundary())
    assert len(uncut) == 1
    assert uncut.area.iloc[0] == pytest.approx(10000)


def test_rail_water_lines_and_polygon_water_area():
    rail = layer([LineString([(X + 50, Y), (X + 50, Y + 100)])])
    water = layer([
        LineString([(X, Y + 50), (X + 100, Y + 50)]),
        box(X + 10, Y + 10, X + 20, Y + 20),
    ])
    result = cut_urban_blocks(boundary(), railways=rail, water=water)
    assert len(result) == 4
    assert result.area.sum() == pytest.approx(9900)
    assert not result.geometry.union_all().intersects(box(X + 11, Y + 11, X + 19, Y + 19))


def test_disjoint_and_hole_preserved():
    hole = Polygon(box(X, Y, X + 100, Y + 100).exterior.coords,
                   [box(X + 20, Y + 20, X + 30, Y + 30).exterior.coords])
    bounds = layer([MultiPolygon([hole, box(X + 200, Y, X + 210, Y + 10)])])
    result = cut_urban_blocks(bounds)
    assert result.area.sum() == pytest.approx(10000)
    assert len(result) == 2


@pytest.mark.parametrize("name, geometries, crs, error", [
    ("boundaries", [box(X, Y, X + 100, Y + 100)], None, "projected metre CRS"),
    ("boundaries", [box(30, 59, 31, 60)], "EPSG:4326", "projected metre CRS"),
    ("boundaries", [Polygon([(X, Y), (X + 10, Y + 10), (X, Y + 10),
                              (X + 10, Y), (X, Y)])], CRS, "invalid"),
    ("roads", [LineString([(X, Y), (X + 1, Y + 1)])], "EPSG:3857", "same metric CRS"),
    ("water", [None], CRS, "null"),
    ("roads", [box(X, Y, X + 1, Y + 1)], CRS, "LineString"),
])
def test_bad_inputs(name, geometries, crs, error):
    data = {"boundaries": boundary(), "roads": None, "water": None}
    data[name] = layer(geometries, crs)
    with pytest.raises(ValueError, match=error):
        cut_urban_blocks(**data)


def test_generator_offline(monkeypatch):
    generator = TopologicalGenerator(boundary().to_crs(4326))
    road = layer([LineString([(X + 50, Y - 10), (X + 50, Y + 110)])]).to_crs(4326)
    monkeypatch.setattr(generator, "_fetch_physical_barriers", lambda: (road, None, None))
    monkeypatch.setattr(generator, "_fetch_hubs", lambda: None)
    result = generator.generate_blocks(min_area_m2=100)
    assert result.crs.to_epsg() == 4326
    assert list(result.columns) == ["block_id", "dist_to_hubs", "geometry"]
    assert result.block_id.tolist() == ["0", "1"]
    assert result.dist_to_hubs.tolist() == [0.0, 0.0]
    with pytest.raises(ValueError, match="No blocks remain"):
        generator.generate_blocks(min_area_m2=6000)


@pytest.mark.parametrize("area", [-1, float("nan"), float("inf")])
def test_bad_min_area_before_fetch(monkeypatch, area):
    generator = TopologicalGenerator(boundary().to_crs(4326))
    monkeypatch.setattr(generator, "_fetch_physical_barriers",
                        lambda: pytest.fail("must validate before network"))
    with pytest.raises(ValueError, match="min_area_m2"):
        generator.generate_blocks(min_area_m2=area)


def test_missing_boundary_crs():
    with pytest.raises(ValueError, match="boundary_gdf must have a CRS"):
        TopologicalGenerator(layer([box(X, Y, X + 100, Y + 100)], None))


@pytest.mark.parametrize("geometries", [[], [None], [Polygon()], [Point(X, Y)]])
def test_generator_rejects_invalid_boundary_before_any_fetch(geometries):
    with pytest.raises(ValueError, match="boundary_gdf"):
        TopologicalGenerator(layer(geometries))


def test_unsupported_blocksnet_version_fails_explicitly(monkeypatch):
    from types import SimpleNamespace
    import src.generation.blocksnet_cutting as adapter
    monkeypatch.setattr(adapter.metadata, "distribution", lambda _: SimpleNamespace(version="2.0.0"))
    with pytest.raises(ImportError, match="1.0.0a3.post14"):
        cut_urban_blocks(boundary())


def test_import_does_not_load_torch():
    proc = subprocess.run([sys.executable, "-c", "import sys; "
                           "import src.generation.topological_generator; "
                           "assert 'torch' not in sys.modules"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
