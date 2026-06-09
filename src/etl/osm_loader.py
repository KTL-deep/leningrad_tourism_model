import osmnx as ox
import geopandas as gpd
import pandas as pd
from shapely.geometry import Polygon, MultiPolygon
import warnings

# Подавляем предупреждения от osmnx об устаревших параметрах (FutureWarning)
warnings.filterwarnings("ignore", category=FutureWarning, module="osmnx")

class OSMLoader:
    """
    Класс для загрузки открытых пространственных данных из OSM.
    Используется для подготовки базовых слоев (границы, сервисы, графы).
    """
    
    def __init__(self, locations=None):
        """
        :param locations: Строка или список строк с названиями локаций.
                          Например: ["Гатчинский район, Ленинградская область", "Пушкинский район, Санкт-Петербург"]
        """
        if locations is None:
            locations = ["Ленинградская область, Россия"]
        
        # Убедимся, что locations всегда список для удобства обработки
        if isinstance(locations, str):
            self.locations = [locations]
        else:
            self.locations = locations
        
        # Настраиваем osmnx: увеличиваем таймаут для больших запросов
        # Для совместимости с новыми и старыми версиями osmnx
        if hasattr(ox.settings, 'requests_timeout'):
            ox.settings.requests_timeout = 200
        else:
            ox.settings.timeout = 200
        
    def get_boundary(self) -> gpd.GeoDataFrame:
        """
        Загрузка полигона(ов) границ исследуемой территории.
        """
        print(f"Загрузка границ для: {self.locations}")
        gdfs = []
        for loc in self.locations:
            try:
                gdf = ox.geocode_to_gdf(loc)
                gdfs.append(gdf[['geometry', 'display_name']])
            except Exception as e:
                print(f"Ошибка загрузки границ для {loc}: {e}")
        
        if not gdfs:
            raise ValueError("Не удалось загрузить ни одной границы.")
            
        combined_gdf = pd.concat(gdfs, ignore_index=True)
        # Объединяем геометрии в единый мультиполигон, если участков несколько
        # Но для дальнейшей работы лучше оставить GeoDataFrame с несколькими строками или объединить через unary_union
        return combined_gdf

    def get_land_use(self, boundary_poly=None) -> gpd.GeoDataFrame:
        """
        Загрузка данных о типах землепользования (Land Use), воды и заболоченности.
        """
        print("Загрузка данных землепользования (Land Use, Природа, Вода, Болота)...")
        tags = {
            'landuse': True, 
            'natural': ['wood', 'water', 'wetland'], 
            'water': True, 
            'waterway': True
        }
        
        if boundary_poly is not None:
            # Используем unary_union для покрытия всей области
            query_poly = boundary_poly.unary_union
            lu_gdf = ox.features_from_polygon(query_poly, tags)
        else:
            # Если полигон не передан, запрашиваем по списку локаций
            gdfs = []
            for loc in self.locations:
                try:
                    gdfs.append(ox.features_from_place(loc, tags))
                except Exception as e:
                    print(f"Ошибка загрузки POI для {loc}: {e}")
            lu_gdf = pd.concat(gdfs, ignore_index=True) if gdfs else gpd.GeoDataFrame()
            
        return lu_gdf
        
    def get_amenities_and_buildings(self, boundary_poly=None) -> gpd.GeoDataFrame:
        """
        Загрузка зданий и инфраструктурных объектов для атрибутирования блоков.
        """
        print("Загрузка объектов для классификации (Еда, Жилье, Транспорт, Культура)...")
        # Оптимизация: вместо True используем конкретные типы, чтобы не качать жилые дома, ларьки и сараи
        tags = {
            'amenity': ['cafe', 'restaurant', 'fast_food', 'bar', 'pub', 'food_court', 'hospital', 'clinic'], 
            'tourism': ['hotel', 'motel', 'hostel', 'guest_house', 'camp_site', 'museum', 'gallery', 'attraction', 'viewpoint', 'information'],
            'historic': True,  # ОКН, памятники, руины, мемориалы
            'leisure': ['park', 'garden', 'nature_reserve'],
            'highway': ['bus_stop'],
            'public_transport': ['station', 'platform'],
            'railway': ['station', 'halt']
        }
        
        if boundary_poly is not None:
            query_poly = boundary_poly.envelope
            amenities_gdf = ox.features_from_polygon(query_poly, tags)
        else:
            gdfs = []
            for loc in self.locations:
                gdfs.append(ox.features_from_place(loc, tags))
            amenities_gdf = pd.concat(gdfs, ignore_index=True) if gdfs else gpd.GeoDataFrame()
            
        if not amenities_gdf.empty:
            # Преобразуем полигоны и линии в точки (центроиды) для унификации объектов POI
            import warnings
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", category=UserWarning)
                if amenities_gdf.crs and not amenities_gdf.crs.is_projected:
                    utm_crs = amenities_gdf.estimate_utm_crs()
                    amenities_gdf['geometry'] = amenities_gdf.to_crs(utm_crs).geometry.centroid.to_crs(amenities_gdf.crs)
                else:
                    amenities_gdf['geometry'] = amenities_gdf.geometry.centroid

        return amenities_gdf

    def get_transport_graph(self, boundary_poly=None, network_type='drive'):
        """
        Загрузка транспортного графа для территории.
        network_type: 'drive', 'walk', 'all', etc.
        Возвращает объект networkx.MultiDiGraph
        """
        print(f"Загрузка графа дорог (тип: {network_type})...")
        if boundary_poly is not None:
            # Для надежности используем буфер или исходный полигон
            G = ox.graph_from_polygon(boundary_poly, network_type=network_type, simplify=True)
        else:
            if len(self.locations) == 1:
                G = ox.graph_from_place(self.locations[0], network_type=network_type, simplify=True)
            else:
                # Если локаций много, лучше скачивать по полигонам, поэтому запросим их
                temp_bounds = self.get_boundary()
                merged_poly = temp_bounds.unary_union
                G = ox.graph_from_polygon(merged_poly, network_type=network_type, simplify=True)
        return G
