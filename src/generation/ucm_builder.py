import geopandas as gpd
import pandas as pd

class UCMBuilder:
    """
    Класс для инициализации Универсальной информационной модели города (UCM).
    Наделяет сгенерированные блоки смыслом через пространственное соединение (Spatial Join).
    """

    def __init__(self, blocks_gdf: gpd.GeoDataFrame):
        """
        :param blocks_gdf: GeoDataFrame со сгенерированными городскими блоками.
        """
        self.blocks = blocks_gdf.copy()
        # Убедимся, что блоки имеют уникальные индексы
        if 'block_id' not in self.blocks.columns:
            self.blocks['block_id'] = range(len(self.blocks))

    def attribute_amenities(self, amenities_gdf: gpd.GeoDataFrame):
        """
        Привязка объектов инфраструктуры к блокам.
        Считает количество объектов каждого типа (атрибут 'amenity', 'building', 'leisure') в блоке.
        """
        if amenities_gdf is None or amenities_gdf.empty:
            return

        print("Атрибутирование OSM сервисов и зданий...")
        # Убедимся, что СКи совпадают
        if self.blocks.crs != amenities_gdf.crs:
            amenities_gdf = amenities_gdf.to_crs(self.blocks.crs)

        # Центроиды, чтобы избежать дублирования на границах
        amenities_pts = amenities_gdf.copy()
        amenities_pts['geometry'] = amenities_pts.geometry.centroid

        joined = gpd.sjoin(amenities_pts, self.blocks, how='inner', predicate='within')
        
        if joined.empty:
            self.blocks['amenity_count'] = 0
            return

        # Подсчет количества amenities внутри каждого блока
        if 'amenity' in joined.columns:
            amenity_counts = joined.groupby('block_id')['amenity'].count().reset_index()
            amenity_counts.rename(columns={'amenity': 'amenity_count'}, inplace=True)
            self.blocks = self.blocks.merge(amenity_counts, on='block_id', how='left')
            self.blocks['amenity_count'] = self.blocks['amenity_count'].fillna(0).astype(int)

    def attribute_cultural_heritage(self, okn_gdf: gpd.GeoDataFrame):
        """
        Привязка Объектов Культурного Наследия (ОКН).
        """
        if okn_gdf is None or okn_gdf.empty:
            return
            
        print("Атрибутирование Объектов культурного наследия (ОКН)...")
        if self.blocks.crs != okn_gdf.crs:
            okn_gdf = okn_gdf.to_crs(self.blocks.crs)
            
        okn_pts = okn_gdf.copy()
        if okn_pts.geometry.type.iloc[0] != 'Point':
            okn_pts['geometry'] = okn_pts.geometry.centroid

        joined = gpd.sjoin(okn_pts, self.blocks, how='inner', predicate='within')
        
        if joined.empty:
            self.blocks['okn_count'] = 0
            return

        okn_counts = joined.groupby('block_id').size().reset_index(name='okn_count')
        self.blocks = self.blocks.merge(okn_counts, on='block_id', how='left')
        self.blocks['okn_count'] = self.blocks['okn_count'].fillna(0).astype(int)

    def attribute_land_use(self, landuse_gdf: gpd.GeoDataFrame):
        """
        Привязка землепользования. Определяет доминирующий тип landuse для блока.
        """
        if landuse_gdf is None or landuse_gdf.empty:
            return
            
        print("Атрибутирование типов землепользования...")
        if self.blocks.crs != landuse_gdf.crs:
            landuse_gdf = landuse_gdf.to_crs(self.blocks.crs)

        intersections = gpd.overlay(self.blocks, landuse_gdf, how='intersection')
        if intersections.empty:
            return
            
        intersections['intersect_area'] = intersections.geometry.area
        
        if 'landuse' in intersections.columns:
            # Находим тип landuse с наибольшей площадью внутри блока
            idx = intersections.groupby('block_id')['intersect_area'].idxmax()
            dominant_lu = intersections.loc[idx, ['block_id', 'landuse']]
            dominant_lu.rename(columns={'landuse': 'dominant_landuse'}, inplace=True)
            self.blocks = self.blocks.merge(dominant_lu, on='block_id', how='left')

    def get_ucm(self) -> gpd.GeoDataFrame:
        """
        Возвращает полностью сгенерированный слой UCM.
        """
        return self.blocks

    def export_to_geojson(self, filepath="data/processed/ucm_blocks.geojson"):
        """
        Сохранение UCM в GeoJSON файл.
        """
        import os
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        print(f"Экспорт UCM модели в {filepath}...")
        self.blocks.to_file(filepath, driver="GeoJSON")
