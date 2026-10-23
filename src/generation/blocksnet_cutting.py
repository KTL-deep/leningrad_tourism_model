"""Small adapter for the unclassified BlocksNet 1.0.0a3.post14 cutting primitive.

The public cutting package imports a mandatory torch classifier even when no
classifier is requested. Load only its self-contained cut.py from the installed
distribution; do not import the package's cutting initializers or patch schemas.
"""

from importlib import metadata, util
import math

import geopandas as gpd


def _cut_blocks_function():
    try:
        distribution = metadata.distribution("blocksnet")
        if distribution.version != "1.0.0a3.post14":
            raise ImportError(f"Unsupported BlocksNet version: {distribution.version}")
        path = distribution.locate_file("blocksnet/blocks/cutting/processing/cut.py")
        if not path.is_file():
            raise FileNotFoundError(path)
        spec = util.spec_from_file_location("_blocksnet_unclassified_cut", path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load {path}")
        module = util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.cut_blocks
    except (metadata.PackageNotFoundError, FileNotFoundError, ImportError, AttributeError) as exc:
        raise ImportError(
            "BlocksNet unclassified cutting requires an installed BlocksNet "
            "1.0.0a3.post14 with blocks/cutting/processing/cut.py"
        ) from exc


def _check_layer(layer, name, crs, kinds, required=False):
    if layer is None:
        if required:
            raise ValueError(f"{name} must be a nonempty GeoDataFrame")
        return gpd.GeoDataFrame(geometry=gpd.GeoSeries([], crs=crs))
    if not isinstance(layer, gpd.GeoDataFrame):
        raise TypeError(f"{name} must be a GeoDataFrame")
    if layer.crs is None or layer.crs != crs:
        raise ValueError(f"{name} must have the same metric CRS as boundaries")
    if required and layer.empty:
        raise ValueError(f"{name} must be nonempty")
    if layer.geometry.isna().any() or layer.geometry.is_empty.any() or not layer.geometry.is_valid.all():
        raise ValueError(f"{name} contains null, empty or invalid geometries")
    if not layer.geometry.geom_type.isin(kinds).all():
        raise ValueError(f"{name} must contain only {', '.join(kinds)} geometries")
    return gpd.GeoDataFrame(geometry=layer.geometry, crs=crs).explode(ignore_index=True)


def cut_urban_blocks(boundaries, roads=None, railways=None, water=None):
    """Cut metric polygon boundaries with line barriers and subtract water areas."""
    if not isinstance(boundaries, gpd.GeoDataFrame):
        raise TypeError("boundaries must be a GeoDataFrame")
    crs = boundaries.crs
    if crs is None or not crs.is_projected or not crs.axis_info or any(
        not math.isclose(axis.unit_conversion_factor, 1.0) for axis in crs.axis_info[:2]
    ):
        raise ValueError("boundaries must have a projected metre CRS")
    boundaries = _check_layer(boundaries, "boundaries", crs, ("Polygon", "MultiPolygon"), required=True)
    roads = _check_layer(roads, "roads", crs, ("LineString", "MultiLineString"))
    railways = _check_layer(railways, "railways", crs, ("LineString", "MultiLineString"))
    water = _check_layer(water, "water", crs, ("LineString", "MultiLineString", "Polygon", "MultiPolygon"))

    lines = gpd.GeoDataFrame(geometry=gpd.GeoSeries(
        list(roads.geometry) + list(railways.geometry) +
        [geom for geom in water.geometry if geom.geom_type == "LineString"], crs=crs
    ))
    polygons = gpd.GeoDataFrame(geometry=gpd.GeoSeries(
        [geom for geom in water.geometry if geom.geom_type == "Polygon"], crs=crs
    ))
    cut = _cut_blocks_function()
    result = cut(boundaries, lines, polygons)
    if not isinstance(result, gpd.GeoDataFrame) or result.crs != crs:
        raise ValueError("BlocksNet cutter returned blocks without the expected metric CRS")
    return _check_layer(result, "blocks", crs, ("Polygon", "MultiPolygon"), required=True)
