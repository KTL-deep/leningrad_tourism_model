import pytest

gpd = pytest.importorskip("geopandas")
pd = pytest.importorskip("pandas")
Point = pytest.importorskip("shapely.geometry").Point
Polygon = pytest.importorskip("shapely.geometry").Polygon

from src.generation.ucm_builder import UCMBuilder


def _simple_sjoin(points_gdf: gpd.GeoDataFrame, polys_gdf: gpd.GeoDataFrame, how: str, predicate: str):
    if how != "inner":
        raise ValueError("This simple test sjoin supports only how='inner'")
    if predicate != "within":
        raise ValueError("This simple test sjoin supports only predicate='within'")

    rows = []
    for left_idx, pt_row in points_gdf.iterrows():
        pt = pt_row.geometry
        for _, poly_row in polys_gdf.iterrows():
            poly = poly_row.geometry
            if pt.within(poly):
                merged = {}
                for c in points_gdf.columns:
                    merged[c] = pt_row[c]
                for c in polys_gdf.columns:
                    merged[c] = poly_row[c]
                rows.append(merged)

    if not rows:
        return gpd.GeoDataFrame(columns=list(points_gdf.columns) + list(polys_gdf.columns), crs=points_gdf.crs)
    return gpd.GeoDataFrame(rows, crs=points_gdf.crs)


def _simple_overlay(blocks_gdf: gpd.GeoDataFrame, landuse_gdf: gpd.GeoDataFrame, how: str, keep_geom_type: bool = True):
    if how != "intersection":
        raise ValueError("This simple test overlay supports only how='intersection'")

    rows = []
    for _, b in blocks_gdf.iterrows():
        for _, lu in landuse_gdf.iterrows():
            inter = b.geometry.intersection(lu.geometry)
            if not inter.is_empty and inter.area > 0:
                row = {"block_id": b["block_id"], "landuse": lu.get("landuse"), "geometry": inter}
                rows.append(row)
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=blocks_gdf.crs)


@pytest.fixture()
def blocks_gdf():
    b0 = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    b1 = Polygon([(10, 0), (20, 0), (20, 10), (10, 10)])
    gdf = gpd.GeoDataFrame(
        {"block_id": [0, 1], "geometry": [b0, b1]},
        geometry="geometry",
        crs="EPSG:4326",
    )
    return gdf


def test_attribute_amenities_counts(monkeypatch, blocks_gdf):
    monkeypatch.setattr(gpd, "sjoin", _simple_sjoin)

    amenities = gpd.GeoDataFrame(
        {
            "amenity": ["cafe", "bank", "cafe"],
            "geometry": [Point(1, 1), Point(11, 1), Point(9, 9)],
        },
        geometry="geometry",
        crs="EPSG:4326",
    )

    builder = UCMBuilder(blocks_gdf)
    builder.attribute_amenities(amenities)
    out = builder.get_ucm().sort_values("block_id")

    counts = dict(zip(out["block_id"], out["poi_count"]))
    assert counts[0] == 2
    assert counts[1] == 1


def test_attribute_cultural_heritage_counts(monkeypatch, blocks_gdf):
    monkeypatch.setattr(gpd, "sjoin", _simple_sjoin)

    okn = gpd.GeoDataFrame(
        {
            "name": ["a", "b", "c"],
            "geometry": [Point(1, 1), Point(2, 2), Point(12, 2)],
        },
        geometry="geometry",
        crs="EPSG:4326",
    )

    builder = UCMBuilder(blocks_gdf)
    builder.attribute_cultural_heritage(okn)
    out = builder.get_ucm().sort_values("block_id")

    counts = dict(zip(out["block_id"], out["okn_count"]))
    assert counts[0] == 2
    assert counts[1] == 1


def test_attribute_land_use_dominant(monkeypatch, blocks_gdf):
    monkeypatch.setattr(gpd, "overlay", _simple_overlay)

    # Для блока 0: residential покрывает 75% площади, forest — 25%
    residential = Polygon([(0, 0), (10, 0), (10, 7.5), (0, 7.5)])
    forest = Polygon([(0, 7.5), (10, 7.5), (10, 10), (0, 10)])

    landuse = gpd.GeoDataFrame(
        {"landuse": ["residential", "forest"], "geometry": [residential, forest]},
        geometry="geometry",
        crs="EPSG:4326",
    )

    builder = UCMBuilder(blocks_gdf)
    builder.attribute_land_use(landuse)
    out = builder.get_ucm()

    dominant = dict(zip(out["block_id"], out.get("dominant_landuse", pd.Series([None] * len(out)))))
    assert dominant[0] == "residential"

