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
        # Инициализируем колонки нулями
        for col in ['amenity_count', 'building_count', 'leisure_count', 'poi_count', 
                    'food_count', 'accommodation_count', 'transport_count']:
            if col not in self.blocks.columns:
                self.blocks[col] = 0

        if amenities_gdf is None or amenities_gdf.empty:
            return
        if self.blocks is None or self.blocks.empty:
            print("Атрибутирование пропущено: список блоков пуст.")
            return

        print("Атрибутирование OSM сервисов и зданий...")
        # Убедимся, что СКи совпадают
        if self.blocks.crs != amenities_gdf.crs:
            amenities_gdf = amenities_gdf.to_crs(self.blocks.crs)

        joined = gpd.sjoin(amenities_gdf, self.blocks, how='inner', predicate='within')
        
        if joined.empty:
            return

        # Общее количество POI/объектов
        poi_counts = joined.groupby('block_id').size().reset_index(name='poi_count_new')
        if 'poi_count' in self.blocks.columns:
            self.blocks = self.blocks.drop(columns=['poi_count'])
        self.blocks = self.blocks.merge(poi_counts, on='block_id', how='left').rename(columns={'poi_count_new': 'poi_count'})
        self.blocks['poi_count'] = self.blocks['poi_count'].fillna(0).astype(int)

        # Семантическая классификация для AHP матрицы
        def count_matches(group, col, values):
            if col not in group.columns:
                return 0
            if callable(values):
                return group[col].apply(values).sum()
            else:
                return group[col].isin(values).sum()

        rows = []
        for block_id, group in joined.groupby('block_id'):
            food = count_matches(group, 'amenity', ['cafe', 'restaurant', 'fast_food', 'bar', 'pub', 'food_court'])
            accomm = count_matches(group, 'tourism', ['hotel', 'motel', 'hostel', 'guest_house', 'apartment', 'camp_site'])
            transport = (
                count_matches(group, 'highway', ['bus_stop']) + 
                count_matches(group, 'public_transport', lambda x: pd.notna(x)) + 
                count_matches(group, 'railway', ['station', 'halt'])
            )
            rows.append({
                'block_id': block_id,
                'food_count_new': int(food),
                'accommodation_count_new': int(accomm),
                'transport_count_new': int(transport)
            })
            
        if rows:
            semantic_counts = pd.DataFrame(rows)
            for c in ['food_count', 'accommodation_count', 'transport_count']:
                if c in self.blocks.columns:
                    self.blocks = self.blocks.drop(columns=[c])
            self.blocks = self.blocks.merge(semantic_counts, on='block_id', how='left').rename(columns={
                'food_count_new': 'food_count',
                'accommodation_count_new': 'accommodation_count',
                'transport_count_new': 'transport_count'
            })
        
        for c in ['food_count', 'accommodation_count', 'transport_count']:
            self.blocks[c] = self.blocks[c].fillna(0).astype(int)

    def attribute_cultural_heritage(self, okn_gdf: gpd.GeoDataFrame):
        """
        Привязка Объектов Культурного Наследия (ОКН).
        """
        if 'okn_count' not in self.blocks.columns:
            self.blocks['okn_count'] = 0

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
            # Перепроецируем для корректного расчета центроида
            local_crs = okn_pts.estimate_utm_crs()
            okn_pts['geometry'] = okn_pts.to_crs(local_crs).geometry.centroid.to_crs(okn_pts.crs)

        joined = gpd.sjoin(okn_pts, self.blocks, how='inner', predicate='within')
        
        if joined.empty:
            return

        okn_counts = joined.groupby('block_id').size().reset_index(name='okn_count_new')
        if 'okn_count' in self.blocks.columns:
            self.blocks = self.blocks.drop(columns=['okn_count'])
        self.blocks = self.blocks.merge(okn_counts, on='block_id', how='left').rename(columns={'okn_count_new': 'okn_count'})
        self.blocks['okn_count'] = self.blocks['okn_count'].fillna(0).astype(int)

    def attribute_protected_areas(self, oopt_gdf: gpd.GeoDataFrame):
        """
        Привязка ООПТ (полигоны). Рассчитывает площадь пересечения с блоком и долю покрытия.
        """
        # Инициализация колонок
        for col in ['oopt_any', 'oopt_area_m2', 'oopt_share']:
            if col not in self.blocks.columns:
                self.blocks[col] = 0.0 if col != 'oopt_any' else False

        if oopt_gdf is None or oopt_gdf.empty:
            return
        if self.blocks is None or self.blocks.empty:
            print("Атрибутирование ООПТ пропущено: список блоков пуст.")
            return

        print("Атрибутирование особо охраняемых природных территорий (ООПТ)...")
        if self.blocks.crs != oopt_gdf.crs:
            oopt_gdf = oopt_gdf.to_crs(self.blocks.crs)

        # Площадь блока (в метрической СК)
        utm_crs = self.blocks.estimate_utm_crs()
        blocks_proj = self.blocks.to_crs(utm_crs) if not self.blocks.crs.is_projected else self.blocks
        blocks_area = pd.DataFrame({
            'block_id': self.blocks['block_id'],
            'block_area_m2': blocks_proj.geometry.area
        })

        # Фильтруем только полигоны для корректной работы overlay и расчета площадей
        oopt_poly = oopt_gdf[oopt_gdf.geometry.type.isin(['Polygon', 'MultiPolygon'])]
        if oopt_poly.empty:
            return

        intersections = gpd.overlay(self.blocks, oopt_poly, how='intersection', keep_geom_type=False)
        if intersections.empty:
            return

        # Считаем площадь в метрической проекции
        if not intersections.crs.is_projected:
            intersections = intersections.to_crs(utm_crs)
            
        intersections['oopt_intersect_area_m2'] = intersections.geometry.area
        oopt_area = (
            intersections.groupby('block_id')['oopt_intersect_area_m2']
            .sum()
            .reset_index()
            .rename(columns={'oopt_intersect_area_m2': 'oopt_area_m2_new'})
        )

        for col in ['oopt_area_m2', 'oopt_share', 'oopt_any']:
            if col in self.blocks.columns:
                self.blocks = self.blocks.drop(columns=[col])

        self.blocks = self.blocks.merge(oopt_area, on='block_id', how='left').rename(columns={'oopt_area_m2_new': 'oopt_area_m2'})
        self.blocks = self.blocks.merge(blocks_area, on='block_id', how='left')

        self.blocks['oopt_area_m2'] = self.blocks['oopt_area_m2'].fillna(0.0)
        self.blocks['oopt_share'] = (self.blocks['oopt_area_m2'] / self.blocks['block_area_m2'].replace({0: pd.NA})).fillna(0.0)
        self.blocks['oopt_any'] = self.blocks['oopt_area_m2'] > 0
        self.blocks = self.blocks.drop(columns=['block_area_m2'])

    def attribute_land_use(self, landuse_gdf: gpd.GeoDataFrame):
        """
        Привязка землепользования. Определяет доминирующий тип landuse для блока.
        """
        # Инициализация
        for col in ['forest_area_m2', 'forest_share', 'dominant_landuse']:
            if col not in self.blocks.columns:
                self.blocks[col] = 0.0 if col != 'dominant_landuse' else "Неизвестно"

        if landuse_gdf is None or landuse_gdf.empty:
            return
        if self.blocks is None or self.blocks.empty:
            print("Атрибутирование землепользования пропущено: список блоков пуст.")
            return
            
        print("Атрибутирование типов землепользования...")
        if self.blocks.crs != landuse_gdf.crs:
            landuse_gdf = landuse_gdf.to_crs(self.blocks.crs)

        blocks_lite = self.blocks[['block_id', 'geometry']].copy()
        landuse_poly = landuse_gdf[landuse_gdf.geometry.type.isin(['Polygon', 'MultiPolygon'])]
        if landuse_poly.empty:
            return

        intersections = gpd.overlay(blocks_lite, landuse_poly, how='intersection', keep_geom_type=False)
        if intersections.empty:
            return
            
        utm_crs = self.blocks.estimate_utm_crs()
        if not intersections.crs.is_projected:
            intersections = intersections.to_crs(utm_crs)
        
        intersections['intersect_area'] = intersections.geometry.area
        
        # Семантика: Доля леса (forest_share)
        forest_mask = pd.Series(False, index=intersections.index)
        if 'landuse' in intersections.columns:
            forest_mask |= (intersections['landuse'] == 'forest')
        if 'natural' in intersections.columns:
            forest_mask |= (intersections['natural'] == 'wood')
            
        forest_inter = intersections[forest_mask]
        if not forest_inter.empty:
            forest_area = forest_inter.groupby('block_id')['intersect_area'].sum().reset_index(name='forest_area_m2_new')
            if 'forest_area_m2' in self.blocks.columns:
                self.blocks = self.blocks.drop(columns=['forest_area_m2'])
            self.blocks = self.blocks.merge(forest_area, on='block_id', how='left').rename(columns={'forest_area_m2_new': 'forest_area_m2'})
            self.blocks['forest_area_m2'] = self.blocks['forest_area_m2'].fillna(0.0)
        else:
            self.blocks['forest_area_m2'] = 0.0
            
        blocks_proj_area = self.blocks.to_crs(utm_crs).geometry.area if not self.blocks.crs.is_projected else self.blocks.geometry.area
        self.blocks['forest_share'] = (self.blocks['forest_area_m2'] / blocks_proj_area).fillna(0.0)
        
        # Доминирующее землепользование
        if 'landuse' in intersections.columns:
            idx = intersections.groupby('block_id')['intersect_area'].idxmax()
            dominant_lu = intersections.loc[idx, ['block_id', 'landuse']]
            if 'dominant_landuse' in self.blocks.columns:
                self.blocks = self.blocks.drop(columns=['dominant_landuse'])
            dominant_lu.rename(columns={'landuse': 'dominant_landuse'}, inplace=True)
            self.blocks = self.blocks.merge(dominant_lu, on='block_id', how='left')
        elif 'dominant_landuse' not in self.blocks.columns:
            self.blocks['dominant_landuse'] = "Неизвестно"

    def attribute_swampiness(self, wetlands_gdf: gpd.GeoDataFrame):
        """
        Привязка заболоченности (natural=wetland). Рассчитывает долю покрытия.
        """
        if 'swamp_share' not in self.blocks.columns:
            self.blocks['swamp_share'] = 0.0

        if wetlands_gdf is None or wetlands_gdf.empty:
            return
            
        print("Атрибутирование заболоченности...")
        if self.blocks.crs != wetlands_gdf.crs:
            wetlands_gdf = wetlands_gdf.to_crs(self.blocks.crs)
            
        blocks_lite = self.blocks[['block_id', 'geometry']].copy()
        wetlands_poly = wetlands_gdf[wetlands_gdf.geometry.type.isin(['Polygon', 'MultiPolygon'])]
        if wetlands_poly.empty:
            return

        intersections = gpd.overlay(blocks_lite, wetlands_poly, how='intersection', keep_geom_type=False)
        if intersections.empty:
            return
            
        # Проецируем для площади
        utm_crs = self.blocks.estimate_utm_crs()
        if not intersections.crs.is_projected:
            intersections = intersections.to_crs(utm_crs)
            
        intersections['swamp_area'] = intersections.geometry.area
        swamp_area = intersections.groupby('block_id')['swamp_area'].sum().reset_index()
        
        if 'swamp_share' in self.blocks.columns:
            self.blocks = self.blocks.drop(columns=['swamp_share'])
            
        self.blocks = self.blocks.merge(swamp_area, on='block_id', how='left')
        self.blocks['swamp_area'] = self.blocks['swamp_area'].fillna(0.0)
        
        blocks_proj_area = self.blocks.to_crs(utm_crs).geometry.area if not self.blocks.crs.is_projected else self.blocks.geometry.area
        self.blocks['swamp_share'] = (self.blocks['swamp_area'] / blocks_proj_area).fillna(0.0)

    def attribute_water_density(self, water_gdf: gpd.GeoDataFrame):
        """
        Привязка плотности водных объектов (реки, озера).
        Рассчитывает суммарную длину рек или площадь озер на кв. км.
        """
        if 'water_density' not in self.blocks.columns:
            self.blocks['water_density'] = 0.0

        if water_gdf is None or water_gdf.empty:
            return
            
        print("Атрибутирование плотности водных объектов...")
        if self.blocks.crs != water_gdf.crs:
            water_gdf = water_gdf.to_crs(self.blocks.crs)
            
        blocks_lite = self.blocks[['block_id', 'geometry']].copy()
        water_poly = water_gdf[water_gdf.geometry.type.isin(['Polygon', 'MultiPolygon'])]
        water_line = water_gdf[water_gdf.geometry.type.isin(['LineString', 'MultiLineString'])]
        
        utm_crs = self.blocks.estimate_utm_crs()
        results = []
        if not water_poly.empty:
            inter_poly = gpd.overlay(blocks_lite, water_poly, how='intersection', keep_geom_type=False)
            if not inter_poly.empty:
                if not inter_poly.crs.is_projected:
                    inter_poly = inter_poly.to_crs(utm_crs)
                inter_poly['water_weight'] = inter_poly.geometry.area
                results.append(inter_poly[['block_id', 'water_weight']])
                
        if not water_line.empty:
            inter_line = gpd.overlay(blocks_lite, water_line, how='intersection', keep_geom_type=False)
            if not inter_line.empty:
                if not inter_line.crs.is_projected:
                    inter_line = inter_line.to_crs(utm_crs)
                inter_line['water_weight'] = inter_line.geometry.length * 10.0 # условная ширина 10м
                results.append(inter_line[['block_id', 'water_weight']])
        
        if not results:
            return
            
        intersections = pd.concat(results)
        water_weight = intersections.groupby('block_id')['water_weight'].sum().reset_index()
        
        if 'water_density' in self.blocks.columns:
            self.blocks = self.blocks.drop(columns=['water_density'])
            
        self.blocks = self.blocks.merge(water_weight, on='block_id', how='left')
        self.blocks['water_weight'] = self.blocks['water_weight'].fillna(0.0)
        
        blocks_proj_area = self.blocks.to_crs(utm_crs).geometry.area if not self.blocks.crs.is_projected else self.blocks.geometry.area
        self.blocks['water_density'] = (self.blocks['water_weight'] / blocks_proj_area).fillna(0.0)

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
        export_gdf = self.blocks.copy()
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
