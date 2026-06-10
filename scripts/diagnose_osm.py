import osmnx as ox
import geopandas as gpd
import os

os.environ['HTTP_PROXY'] = ''
os.environ['HTTPS_PROXY'] = ''
os.environ['NO_PROXY'] = '*'

pilot_regions = [
    "Пушкин, Санкт-Петербург, Россия",
    "город Пушкин, Санкт-Петербург",
    "Пушкин, город Пушкин",
    "Pushkin, Saint Petersburg",
    "Gatchina, Russia"
]

for loc in pilot_regions:
    print(f"Checking {loc}...")
    try:
        gdf = ox.geocode_to_gdf(loc)
        print(f"  Boundary area: {gdf.to_crs(gdf.estimate_utm_crs()).geometry.area.sum() / 1e6:.2f} sq km")
        print(f"  Geometry type: {gdf.geometry.type.unique()}")
        
        tags = {'highway': True}
        roads = ox.features_from_polygon(gdf.unary_union, tags)
        print(f"  Total roads found with 'highway': {len(roads)}")
        
        specific_tags = {
            'highway': ['motorway', 'trunk', 'primary', 'secondary', 'tertiary', 
                        'unclassified', 'residential']
        }
        roads_spec = ox.features_from_polygon(gdf.unary_union, specific_tags)
        print(f"  Roads found with specific tags: {len(roads_spec)}")
        
    except Exception as e:
        print(f"  Error: {e}")
