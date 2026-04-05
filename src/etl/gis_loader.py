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

    def load_protected_areas(self, filename="oopt.geojson") -> gpd.GeoDataFrame:
        """
        Загрузка особо охраняемых природных территорий (ООПТ).
        :param filename: Имя файла в директории данных
        :return: GeoDataFrame с полигонами ООПТ
        """
        filepath = os.path.join(self.data_dir, filename)
        if not os.path.exists(filepath):
            print(f"Внимание: файл ООПТ не найден по пути {filepath}. Возвращен пустой GeoDataFrame.")
            return gpd.GeoDataFrame(columns=["geometry"], geometry="geometry", crs="EPSG:4326")

        print(f"Загрузка ООПТ из {filepath}...")
        gdf = gpd.read_file(filepath)
        return gdf

    # ------------------------------------------------------------------
    # Кадастровые данные (участки/кварталы)
    # ------------------------------------------------------------------

    def load_cadastral_data(self, filename="cadastre.geojson") -> gpd.GeoDataFrame:
        """
        Загрузка кадастровых участков/кварталов из локального файла.
        :param filename: Имя файла в директории данных
        :return: GeoDataFrame с полигонами кадастрового деления
        """
        filepath = os.path.join(self.data_dir, filename)
        if not os.path.exists(filepath):
            print(f"Внимание: файл кадастра не найден по пути {filepath}. Возвращен пустой GeoDataFrame.")
            return gpd.GeoDataFrame(columns=["geometry"], geometry="geometry", crs="EPSG:4326")

        print(f"Загрузка кадастровых участков из {filepath}...")
        try:
            gdf = gpd.read_file(filepath)
            # Базовая очистка
            gdf.geometry = gdf.geometry.make_valid()
            return gdf[gdf.geometry.is_valid & ~gdf.geometry.is_empty]
        except Exception as e:
            print(f"Ошибка при загрузке кадастра: {e}")
            return gpd.GeoDataFrame(columns=["geometry"], geometry="geometry", crs="EPSG:4326")

    # ------------------------------------------------------------------
    # DEM — Цифровая модель рельефа
    # ------------------------------------------------------------------

    def load_dem(self, filename="dem.tif"):
        """
        Загрузка цифровой модели рельефа (DEM).
        Для растровой обработки требуется библиотека rasterio.
        В текущей реализации возвращает путь до файла.
        """
        filepath = os.path.join(self.data_dir, filename)
        if not os.path.exists(filepath):
            print(f"Внимание: файл DEM не найден по пути {filepath}.")
            return None

        print(f"DEM файл доступен по пути {filepath}.")
        return filepath
