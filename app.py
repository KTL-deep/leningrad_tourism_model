import streamlit as st
import geopandas as gpd
import folium
from streamlit_folium import st_folium
import json

# Конфигурация страницы должна быть первым вызовом
st.set_page_config(
    page_title="Leningrad Tourism Model",
    page_icon="🗺️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Темная тема (Стиль premium)
st.markdown("""
    <style>
    .main { background-color: #0e1117; }
    h1, h2, h3 { color: #f0f2f6; }
    </style>
""", unsafe_allow_html=True)

# Цветовая палитра для типов землепользования
LANDUSE_COLORS = {
    "Парк/Рекреация": "#2ca02c",      # Зеленый
    "Жилая застройка": "#8c564b",     # Коричневый
    "Коммерция/Услуги": "#ff7f0e",    # Оранжевый
    "Инфраструктурный Хаб": "#1f77b4" # Синий
}

@st.cache_data
def load_data():
    file_path = "data/processed/ucm_blocks_optimized.geojson"
    try:
        gdf = gpd.read_file(file_path)
        # Принудительно приводим к EPSG:4326 для Folium
        if gdf.crs and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)
        
        gdf['dominant_landuse'] = gdf['dominant_landuse'].fillna("Неизвестно")
        gdf['forest_share'] = (gdf['forest_share'].fillna(0) * 100).round(1).astype(str) + "%"
        
        # ОСТАВЛЯЕМ ТОЛЬКО НУЖНЫЕ ДЛЯ КАРТЫ КОЛОНКИ
        keep_cols = [
            'block_id', 
            'dominant_landuse', 
            'accommodation_count', 
            'food_count', 
            'transport_count', 
            'forest_share',
            'geometry',
            # Сценарные колонки
            'Target_LandUse_Экоцентризм', 'Capacity_Экоцентризм', 'S_ik_Экоцентризм_Парк_Рекреация', 'S_ik_Экоцентризм_Жилая_застройка', 'S_ik_Экоцентризм_Коммерция_Услуги', 'S_ik_Экоцентризм_Инфраструктурный_Хаб',
            'Target_LandUse_Историко-центризм', 'Capacity_Историко-центризм', 'S_ik_Историко_центризм_Парк_Рекреация', 'S_ik_Историко_центризм_Жилая_застройка', 'S_ik_Историко_центризм_Коммерция_Услуги', 'S_ik_Историко_центризм_Инфраструктурный_Хаб',
            'Target_LandUse_Инфраструктурный', 'Capacity_Инфраструктурный', 'S_ik_Инфраструктурный_Парк_Рекреация', 'S_ik_Инфраструктурный_Жилая_застройка', 'S_ik_Инфраструктурный_Коммерция_Услуги', 'S_ik_Инфраструктурный_Инфраструктурный_Хаб'
        ]
        
        existing_cols = [c for c in keep_cols if c in gdf.columns]
        gdf = gdf[existing_cols]
        
        return gdf
    except Exception as e:
        st.error(f"Ошибка загрузки данных: {e}. Пожалуйста, запустите main.py для генерации файла.")
        return None

def main():
    st.title("🗺️ Интерактивная модель: Туристический потенциал территорий")
    st.markdown("Предиктивная визуализация оптимального землепользования (Target Land Use) с применением алгоритма Simulated Annealing.")
    
    gdf = load_data()
    if gdf is None or gdf.empty:
        return
        
    # --- БОКОВАЯ ПАНЕЛЬ (SIDEBAR) ---
    st.sidebar.header("⚙️ Параметры отображения")
    
    total_blocks = len(gdf)
    st.sidebar.metric("Всего полигонов (блоков):", total_blocks)
    
    scenario = st.sidebar.selectbox(
        "Выберите сценарий развития:",
        ["Экоцентризм", "Историко-центризм", "Инфраструктурный"]
    )
    
    st.sidebar.markdown("---")
    st.sidebar.markdown("### Легенда (Target Land Use)")
    for lu, color in LANDUSE_COLORS.items():
        st.sidebar.markdown(f'<div style="display:flex; align-items:center;"><div style="width:20px; height:20px; background-color:{color}; border-radius:4px; margin-right:10px;"></div><b>{lu}</b></div>', unsafe_allow_html=True)

    # Подготавливаем данные для выбранного сценария
    scenario_slug = scenario.replace("-", "_").replace(" ", "_")
    target_col = f"Target_LandUse_{scenario}"
    capacity_col = f"Capacity_{scenario}"
    
    # Копируем нужные колонки в общие имена для фолиума
    display_gdf = gdf.copy()
    display_gdf['Current_Target_LandUse'] = display_gdf[target_col]
    display_gdf['Current_Capacity'] = display_gdf[capacity_col].astype(str) + " чел."
    
    # Извлекаем максимальный балл пригодности для выбранного типа
    display_gdf['Max_Suitability'] = display_gdf.apply(
        lambda row: round(row.get(f"S_ik_{scenario_slug}_{row[target_col].replace('/', '_').replace(' ', '_')}", 0.0), 4),
        axis=1
    )

    # --- КАРТА FOLIUM ---
    bounds = display_gdf.total_bounds
    center_lat = (bounds[1] + bounds[3]) / 2
    center_lon = (bounds[0] + bounds[2]) / 2

    # Используем премиальную темную подложку
    m = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=14,
        tiles="CartoDB dark_matter"
    )

    def style_function(feature):
        target_lu = feature['properties'].get('Current_Target_LandUse', "Неизвестно")
        color = LANDUSE_COLORS.get(target_lu, "#808080")
        return {
            'fillColor': color,
            'color': '#ffffff',  # белые границы
            'weight': 0.5,
            'fillOpacity': 0.75
        }

    def highlight_function(feature):
        return {
            'fillColor': '#ffffff',  # Белый при наведении
            'color': '#ffffff',
            'weight': 2,
            'fillOpacity': 0.9
        }

    folium.GeoJson(
        display_gdf,
        style_function=style_function,
        highlight_function=highlight_function,
        tooltip=folium.GeoJsonTooltip(
            fields=[
                'block_id', 
                'Current_Target_LandUse', 
                'Max_Suitability', 
                'Current_Capacity', 
                'dominant_landuse',
                'accommodation_count', 
                'food_count', 
                'transport_count'
            ],
            aliases=[
                '🆔 ID Блока:', 
                '🎯 Предписанный тип (Target):', 
                '📈 Балл пригодности (S_ik):', 
                '⚡ Расчетная емкость (Capacity):', 
                '🏙️ Текущее землепользование:',
                '🛏️ Места размещения:', 
                '🍽️ Точки питания:', 
                '🚌 Транспорт:'
            ],
            labels=True,
            sticky=False,
            style="""
                background-color: #2b2b2b;
                color: #f0f0f0;
                font-family: "Inter", sans-serif;
                font-size: 13px;
                padding: 10px;
                border: 1px solid #444;
                border-radius: 6px;
                box-shadow: 3px 3px 10px rgba(0,0,0,0.5);
            """
        )
    ).add_to(m)

    # Отрисовка в веб-интерфейсе
    st_folium(m, width="100%", height=700, returned_objects=[])

if __name__ == "__main__":
    main()
