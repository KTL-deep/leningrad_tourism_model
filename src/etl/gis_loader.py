import geopandas as gpd
import os

class GISLoader:
    """
    Класс для загрузки экспертных региональных данных (ОКН, ООПТ, DEM).
    Ожидается, что данные будут предоставлены в форматах GeoJSON, Shapefile и т.д.
    """
    
    def __init__(self, data_dir="data/raw"):
        self.data_dir = data_dir
        
    def load_cultural_heritage(self, filename="okn.geojson") -> gpd.GeoDataFrame:
        """
        Загрузка точечных объектов культурного наследия.
        :param filename: Имя файла в директории данных
        :return: GeoDataFrame с объектами ОКН
        """
        filepath = os.path.join(self.data_dir, filename)
        if not os.path.exists(filepath):
            print(f"Внимание: файл ОКН не найден по пути {filepath}. Возвращен пустой GeoDataFrame.")
            # Возвращаем пустой GeoDataFrame
            return gpd.GeoDataFrame(columns=['geometry'], geometry='geometry')
            
        print(f"Загрузка объектов культурного наследия из {filepath}...")
        gdf = gpd.read_file(filepath)
        return gdf

    def load_protected_areas(self, filename="oopt.geojson") -> gpd.GeoDataFrame:
        """
        Загрузка особо охраняемых природных территорий (ООПТ).
        :param filename: Имя файла в директории данных
        :return: GeoDataFrame с полигонами ООПТ
        """
        filepath = os.path.join(self.data_dir, filename)
        if not os.path.exists(filepath):
            print(f"Внимание: файл ООПТ не найден по пути {filepath}. Возвращен пустой GeoDataFrame.")
            return gpd.GeoDataFrame(columns=['geometry'], geometry='geometry')
            
        print(f"Загрузка ООПТ из {filepath}...")
        gdf = gpd.read_file(filepath)
        return gdf

    def load_dem(self, filename="dem.tif"):
        """
        Загрузка цифровой модели рельефа (DEM).
        Требуется библиотека rasterio (если планируется обработка).
        В текущей реализации возвращает путь до файла.
        """
        filepath = os.path.join(self.data_dir, filename)
        if not os.path.exists(filepath):
            print(f"Внимание: файл DEM не найден по пути {filepath}.")
            return None
        
        print(f"DEM файл доступен по пути {filepath}.")
        return filepath
