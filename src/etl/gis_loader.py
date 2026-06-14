import geopandas as gpd
import pandas as pd
import requests
import os
from shapely.geometry import Point


class GISLoader:
    """
    Класс для загрузки экспертных региональных данных (ОКН, ООПТ, DEM).
    Поддерживает как локальные файлы (GeoJSON/Shapefile), так и
    выгрузку из открытых источников (реестры Министерства культуры).
    """

    def __init__(self, data_dir="data/raw"):
        self.data_dir = data_dir

    # ------------------------------------------------------------------
    # ОКН — Объекты культурного наследия
    # ------------------------------------------------------------------

    def load_cultural_heritage(self, filename="okn.geojson") -> gpd.GeoDataFrame:
        """
        Загрузка точечных объектов культурного наследия из локального файла.
        Если файл не найден — пробует загрузить из открытого реестра ОКН через API.

        :param filename: Имя файла в директории данных
        :return: GeoDataFrame с объектами ОКН (CRS EPSG:4326)
        """
        filepath = os.path.join(self.data_dir, filename)
        if os.path.exists(filepath):
            print(f"Загрузка объектов культурного наследия из {filepath}...")
            return gpd.read_file(filepath)

        print(f"Файл ОКН не найден ({filepath}). Пробуем загрузить из открытого реестра...")
        gdf = self._fetch_okn_from_mkrf_api()
        if not gdf.empty:
            # Кэшируем для повторного использования
            os.makedirs(self.data_dir, exist_ok=True)
            gdf.to_file(filepath, driver="GeoJSON")
            print(f"Реестр ОКН сохранён в {filepath} ({len(gdf)} объектов).")
        return gdf

    def _fetch_okn_from_mkrf_api(self, region_code="47") -> gpd.GeoDataFrame:
        """
        Загрузка реестра ОКН Ленинградской области из открытого API
        Министерства культуры РФ (opendata.mkrf.ru).

        :param region_code: Код региона (47 — Ленинградская область)
        :return: GeoDataFrame с точечными объектами ОКН
        """
        # Открытый датасет ОКН на портале data.gov.ru / mkrf.ru
        # Документация: https://opendata.mkrf.ru/opendata/7705851331-egrkn
        api_url = (
            "https://opendata.mkrf.ru/opendata/7705851331-egrkn/"
            f"meta.json"
        )
        rows = []
        try:
            print("  Загрузка реестра ОКН через opendata.mkrf.ru...")
            # Пробуем альтернативный источник — EGRKN CSV (публичный датасет)
            csv_url = (
                "https://raw.githubusercontent.com/opendata-mkrf/"
                "egrkn/main/data/egrkn.csv"
            )
            response = requests.get(csv_url, timeout=30)
            if response.status_code == 200:
                from io import StringIO
                df = pd.read_csv(StringIO(response.text), sep=";", on_bad_lines="skip")
                # Фильтруем по региону
                region_col = next(
                    (c for c in df.columns if "регион" in c.lower() or "region" in c.lower()), None
                )
                lat_col = next(
                    (c for c in df.columns if c.lower() in ("lat", "latitude", "широта")), None
                )
                lon_col = next(
                    (c for c in df.columns if c.lower() in ("lon", "lng", "longitude", "долгота")), None
                )

                if lat_col and lon_col:
                    if region_col:
                        df = df[df[region_col].astype(str).str.contains("Ленинград", na=False)]
                    df = df.dropna(subset=[lat_col, lon_col])
                    df[lat_col] = pd.to_numeric(df[lat_col], errors="coerce")
                    df[lon_col] = pd.to_numeric(df[lon_col], errors="coerce")
                    df = df.dropna(subset=[lat_col, lon_col])
                    geometry = [Point(lon, lat) for lon, lat in zip(df[lon_col], df[lat_col])]
                    gdf = gpd.GeoDataFrame(df, geometry=geometry, crs="EPSG:4326")
                    print(f"  Загружено {len(gdf)} объектов ОКН Ленинградской области.")
                    return gdf
        except Exception as e:
            print(f"  Не удалось загрузить реестр ОКН автоматически: {e}")

        print(
            "  ⚠️  Автоматическая загрузка ОКН недоступна. "
            "Поместите файл okn.geojson в папку data/raw/ вручную."
        )
        return gpd.GeoDataFrame(columns=["geometry"], geometry="geometry", crs="EPSG:4326")

    # ------------------------------------------------------------------
    # ООПТ — Особо охраняемые природные территории
    # ------------------------------------------------------------------

    def load_protected_areas(self, filename="oopt.geojson", boundary_poly=None) -> gpd.GeoDataFrame:
        """
        Загрузка особо охраняемых природных территорий (ООПТ).
        Если локальный файл не найден — пробует скачать из OpenStreetMap по границам.

        :param filename: Имя файла в директории данных
        :param boundary_poly: Полигон границ исследуемой территории (Shapely Polygon)
        :return: GeoDataFrame с полигонами ООПТ
        """
        filepath = os.path.join(self.data_dir, filename)
        if os.path.exists(filepath):
            print(f"Загрузка ООПТ из {filepath}...")
            return gpd.read_file(filepath)

        if boundary_poly is not None:
            print("Локальный файл ООПТ не найден. Скачиваем данные ООПТ из OpenStreetMap...")
            import osmnx as ox
            tags = {
                'boundary': 'protected_area',
                'leisure': 'nature_reserve'
            }
            try:
                oopt_gdf = ox.features_from_polygon(boundary_poly, tags)
                # Оставляем только полигоны
                oopt_gdf = oopt_gdf[oopt_gdf.geometry.type.isin(['Polygon', 'MultiPolygon'])]
                if not oopt_gdf.empty:
                    oopt_gdf = oopt_gdf[['geometry']].reset_index(drop=True)
                    os.makedirs(self.data_dir, exist_ok=True)
                    oopt_gdf.to_file(filepath, driver="GeoJSON")
                    print(f"Данные ООПТ успешно скачаны из OSM и сохранены в {filepath}")
                    return oopt_gdf
            except Exception as e:
                print(f"Не удалось скачать ООПТ из OSM: {e}")

        print(f"Внимание: файл ООПТ не найден по пути {filepath}. Возвращен пустой GeoDataFrame.")
        return gpd.GeoDataFrame(columns=["geometry"], geometry="geometry", crs="EPSG:4326")

    # ------------------------------------------------------------------
    # DEM — Цифровая модель рельефа
    # ------------------------------------------------------------------

    def load_dem(self, filename="dem.tif", boundary_poly=None):
        """
        Загрузка цифровой модели рельефа (DEM).
        1. Если файл уже существует по пути `data/raw/dem.tif`, возвращает его.
        2. Если передан `boundary_poly`, сначала ищет локальные снимки в папке `data/raw/dem/`.
           Находит снимки, пересекающиеся с границей, объединяет (mosaic) и обрезает (clip) их.
        3. Если локальных снимков нет, скачивает обрезанный растр через API OpenTopography.
        """
        filepath = os.path.join(self.data_dir, filename)
        
        # 1. Если файл уже есть на диске
        if os.path.exists(filepath):
            print(f"DEM файл доступен по пути {filepath}.")
            return filepath

        # 2. Если передан полигон границ
        if boundary_poly is not None:
            dem_dir = os.path.join(self.data_dir, "dem")
            
            # Проверяем наличие папки с локальными снимками
            if os.path.exists(dem_dir):
                tif_files = [
                    os.path.join(dem_dir, f) 
                    for f in os.listdir(dem_dir) 
                    if f.lower().endswith(('.tif', '.tiff'))
                ]
                
                if tif_files:
                    print(f"Найдены локальные DEM-файлы в {dem_dir}. Проверка пересечения...")
                    try:
                        import rasterio
                        from rasterio.merge import merge
                        from rasterio.mask import mask
                        from shapely.geometry import box
                        
                        overlapping_srcs = []
                        boundary_bbox = box(*boundary_poly.bounds)
                        
                        for tif_path in tif_files:
                            src = rasterio.open(tif_path)
                            raster_bbox = box(*src.bounds)
                            if boundary_bbox.intersects(raster_bbox):
                                overlapping_srcs.append(src)
                                print(f"  Файл {os.path.basename(tif_path)} пересекается с территорией.")
                            else:
                                src.close()
                                
                        if overlapping_srcs:
                            print(f"Объединение и обрезка {len(overlapping_srcs)} снимков...")
                            # Объединяем (mosaic)
                            mosaic, out_trans = merge(overlapping_srcs)
                            out_meta = overlapping_srcs[0].meta.copy()
                            
                            # Закрываем исходные файлы
                            for src in overlapping_srcs:
                                src.close()
                                
                            # Сохраняем временную мозаику
                            temp_mosaic_path = os.path.join(self.data_dir, "temp_mosaic.tif")
                            out_meta.update({
                                "driver": "GTiff",
                                "height": mosaic.shape[1],
                                "width": mosaic.shape[2],
                                "transform": out_trans
                            })
                            with rasterio.open(temp_mosaic_path, "w", **out_meta) as dest:
                                dest.write(mosaic)
                                
                            # Теперь обрезаем по полигону
                            with rasterio.open(temp_mosaic_path) as temp_src:
                                out_image, out_transform = mask(temp_src, [boundary_poly], crop=True)
                                out_meta = temp_src.meta.copy()
                                
                            # Удаляем временную мозаику
                            if os.path.exists(temp_mosaic_path):
                                os.remove(temp_mosaic_path)
                                
                            # Сохраняем итоговый обрезанный файл
                            out_meta.update({
                                "driver": "GTiff",
                                "height": out_image.shape[1],
                                "width": out_image.shape[2],
                                "transform": out_transform
                            })
                            
                            os.makedirs(os.path.dirname(filepath), exist_ok=True)
                            with rasterio.open(filepath, "w", **out_meta) as dest:
                                dest.write(out_image)
                                
                            print(f"Локальный DEM успешно собран и обрезан в {filepath}")
                            return filepath
                        else:
                            print("Ни один локальный DEM-файл не пересекается с выбранной территорией.")
                    except ImportError:
                        print("Библиотека rasterio не установлена. Локальная обработка DEM невозможна.")
                    except Exception as e:
                        print(f"Ошибка локальной обработки DEM: {e}")
            else:
                print(f"Папка {dem_dir} не найдена. Пробуем скачать DEM через API...")
                
            # 3. Скачивание через API OpenTopography
            print("Запуск автозагрузки SRTM 30m с OpenTopography API...")
            
            # Проверяем наличие API-ключа OpenTopography в настройках, файлах или окружении
            api_key = os.environ.get("OPENTOPOGRAPHY_API_KEY")
            
            if not api_key:
                # 1. Попытка импорта из локального конфига configs/settings.py
                try:
                    from configs.settings import OPENTOPOGRAPHY_API_KEY as key
                    if key and key != "ВСТАВЬТЕ_ВАШ_КЛЮЧ_ЗДЕСЬ":
                        api_key = key
                except ImportError:
                    pass
            
            if not api_key:
                # 2. Попытка парсинга из configs/settings.template.py (если пользователь отредактировал его)
                template_path = os.path.join(os.path.dirname(__file__), "..", "..", "configs", "settings.template.py")
                if os.path.exists(template_path):
                    try:
                        with open(template_path, "r", encoding="utf-8") as tf:
                            import re
                            content = tf.read()
                            match = re.search(r"OPENTOPOGRAPHY_API_KEY\s*=\s*['\"]([^'\"]+)['\"]", content)
                            if match and match.group(1) != "ВСТАВЬТЕ_ВАШ_КЛЮЧ_ЗДЕСЬ":
                                api_key = match.group(1)
                    except Exception as e:
                        print(f"Не удалось спарсить ключ из {template_path}: {e}")
            
            if not api_key:
                # 3. Попытка чтения из текстового файла в data/raw/
                key_file_path = os.path.join(self.data_dir, "opentopography_key.txt")
                if os.path.exists(key_file_path):
                    try:
                        with open(key_file_path, "r", encoding="utf-8") as kf:
                            api_key = kf.read().strip()
                    except Exception as e:
                        print(f"Не удалось прочитать ключ из {key_file_path}: {e}")
            
            if not api_key:
                key_file_path = os.path.join(self.data_dir, "opentopography_key.txt")
                print("\n" + "="*80)
                print("[!] ВНИМАНИЕ: Для скачивания рельефа (DEM) через API требуется бесплатный API-ключ OpenTopography.")
                print("    1. Зарегистрируйтесь на сайте: https://portal.opentopography.org/")
                print("    2. Скопируйте ваш API-ключ из раздела 'My Account' -> 'myAPIKey'.")
                print(f"    3. Сохраните ключ вconfigs/settings.py или в текстовый файл: {key_file_path}")
                print("    Загрузка DEM пропущена.")
                print("="*80 + "\n")
                return None

            bounds = boundary_poly.bounds  # (minx, miny, maxx, maxy)
            west, south, east, north = bounds
            
            url = "https://portal.opentopography.org/API/globaldem"
            params = {
                "demtype": "SRTMGL1",
                "south": south,
                "north": north,
                "west": west,
                "east": east,
                "outputFormat": "GTiff",
                "apikey": api_key
            }
            
            try:
                response = requests.get(url, params=params, stream=True, timeout=120)
                if response.status_code == 200:
                    os.makedirs(os.path.dirname(filepath), exist_ok=True)
                    with open(filepath, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            f.write(chunk)
                    print(f"Растр DEM успешно скачан и сохранен в {filepath}")
                    return filepath
                else:
                    print(f"Ошибка OpenTopography API (Код {response.status_code}): {response.text}")
            except Exception as e:
                print(f"Не удалось скачать DEM через API: {e}")

        print(f"Внимание: файл DEM не найден по пути {filepath}.")
        return None
