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
        if self.blocks is None or self.blocks.empty:
            print("Атрибутирование пропущено: список блоков пуст.")
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
            self.blocks['building_count'] = 0
            self.blocks['leisure_count'] = 0
            self.blocks['poi_count'] = 0
            return

        # Общее количество POI/объектов
        poi_counts = joined.groupby('block_id').size().reset_index(name='poi_count')
        self.blocks = self.blocks.merge(poi_counts, on='block_id', how='left')
        self.blocks['poi_count'] = self.blocks['poi_count'].fillna(0).astype(int)

        # Подсчет по типам (колонки могут отсутствовать)
        for src_col, out_col in (
            ('amenity', 'amenity_count'),
            ('building', 'building_count'),
            ('leisure', 'leisure_count'),
        ):
            if src_col in joined.columns:
                counts = joined[joined[src_col].notna()].groupby('block_id')[src_col].count().reset_index()
                counts.rename(columns={src_col: out_col}, inplace=True)
                self.blocks = self.blocks.merge(counts, on='block_id', how='left')
            else:
                self.blocks[out_col] = 0
            self.blocks[out_col] = self.blocks[out_col].fillna(0).astype(int)

    def attribute_cultural_heritage(self, okn_gdf: gpd.GeoDataFrame):
        """
        Привязка Объектов Культурного Наследия (ОКН).
        """
        if okn_gdf is None or okn_gdf.empty:
            return
        if self.blocks is None or self.blocks.empty:
            print("Атрибутирование ОКН пропущено: список блоков пуст.")
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

    def attribute_protected_areas(self, oopt_gdf: gpd.GeoDataFrame):
        """
        Привязка ООПТ (полигоны). Рассчитывает площадь пересечения с блоком и долю покрытия.
        """
        if oopt_gdf is None or oopt_gdf.empty:
            return
        if self.blocks is None or self.blocks.empty:
            print("Атрибутирование ООПТ пропущено: список блоков пуст.")
            return

        print("Атрибутирование особо охраняемых природных территорий (ООПТ)...")
        if self.blocks.crs != oopt_gdf.crs:
            oopt_gdf = oopt_gdf.to_crs(self.blocks.crs)

        # Площадь блока (в метрической СК, т.к. блоки приходят из генератора в UTM)
        blocks_area = self.blocks[['block_id', 'geometry']].copy()
        blocks_area['block_area_m2'] = blocks_area.geometry.area

        intersections = gpd.overlay(self.blocks, oopt_gdf, how='intersection', keep_geom_type=False)
        if intersections.empty:
            self.blocks['oopt_any'] = False
            self.blocks['oopt_area_m2'] = 0.0
            self.blocks['oopt_share'] = 0.0
            return

        intersections['oopt_intersect_area_m2'] = intersections.geometry.area
        oopt_area = (
            intersections.groupby('block_id')['oopt_intersect_area_m2']
            .sum()
            .reset_index()
            .rename(columns={'oopt_intersect_area_m2': 'oopt_area_m2'})
        )

        self.blocks = self.blocks.merge(oopt_area, on='block_id', how='left')
        self.blocks = self.blocks.merge(blocks_area[['block_id', 'block_area_m2']], on='block_id', how='left')

        self.blocks['oopt_area_m2'] = self.blocks['oopt_area_m2'].fillna(0.0)
        # защита от деления на ноль
        self.blocks['oopt_share'] = (self.blocks['oopt_area_m2'] / self.blocks['block_area_m2'].replace({0: pd.NA})).fillna(0.0)
        self.blocks['oopt_any'] = self.blocks['oopt_area_m2'] > 0

    def attribute_land_use(self, landuse_gdf: gpd.GeoDataFrame):
        """
        Привязка землепользования. Определяет доминирующий тип landuse для блока.
        """
        if landuse_gdf is None or landuse_gdf.empty:
            return
        if self.blocks is None or self.blocks.empty:
            print("Атрибутирование землепользования пропущено: список блоков пуст.")
            return
            
        print("Атрибутирование типов землепользования...")
        if self.blocks.crs != landuse_gdf.crs:
            landuse_gdf = landuse_gdf.to_crs(self.blocks.crs)

        intersections = gpd.overlay(self.blocks, landuse_gdf, how='intersection', keep_geom_type=False)
        if intersections.empty:
            return

        # Оставляем только площадные геометрии для корректного расчёта площади
        intersections = intersections[
            intersections.geometry.type.isin(['Polygon', 'MultiPolygon'])
        ]
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
        if self.blocks is None or self.blocks.empty:
            print("⚠️  Экспорт пропущен: список блоков пуст. Проверьте входные данные.")
            return
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        print(f"Экспорт UCM модели в {filepath}...")
        export_gdf = self.blocks
        # GeoJSON удобнее хранить в EPSG:4326 для QGIS/kepler.gl
        try:
            if export_gdf.crs is not None and export_gdf.crs.to_epsg() != 4326:
                export_gdf = export_gdf.to_crs(epsg=4326)
        except Exception:
            pass
        try:
            export_gdf.to_file(filepath, driver="GeoJSON")
        except PermissionError:
            from datetime import datetime

            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            alt_path = f"{filepath}.locked-{ts}.geojson"
            print(f"⚠️  Не удалось перезаписать {filepath} (файл занят). Пишем в {alt_path}")
            export_gdf.to_file(alt_path, driver="GeoJSON")
