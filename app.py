import streamlit as st
import geopandas as gpd
import pandas as pd
import folium
from streamlit_folium import st_folium
import json
import numpy as np

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
    .stMetric { background-color: #1e2129; padding: 10px; border-radius: 5px; }
    </style>
""", unsafe_allow_html=True)

# Цветовая палитра для элементов ТОКТ (согласно Lutchenko)
LANDUSE_COLORS = {
    "Опорные центры (Хабы)": "#800080",      # Фиолетовый
    "Локальные точки притяжения": "#ff7f0e", # Оранжевый
    "Линейные элементы (Маршруты)": "#2ca02c", # Зеленый
    "Неизвестно": "#808080"
}

@st.cache_data
def load_data():
    file_path = "data/processed/ucm_blocks_optimized.geojson"
    try:
        gdf = gpd.read_file(file_path)
        # Принудительно приводим к EPSG:4326 для Folium
        if gdf.crs and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)
        
        # Автоматический подбор всех колонок S_ik
        s_ik_cols = [c for c in gdf.columns if c.startswith("S_ik_")]
        
        keep_cols = [
            'block_id', 
            'dominant_landuse', 
            'accommodation_count', 
            'food_count', 
            'transport_count', 
            'forest_share',
            'swamp_share',
            'water_density',
            'dist_to_hubs',
            'geometry'
        ]
        
        # Добавляем сценарные колонки
        for scenario in ["Экоцентризм", "Историко-центризм", "Инфраструктурный"]:
            target_col = f"Target_LandUse_{scenario}"
            cap_col = f"Capacity_{scenario}"
            if target_col in gdf.columns: keep_cols.append(target_col)
            if cap_col in gdf.columns: keep_cols.append(cap_col)
        
        keep_cols.extend(s_ik_cols)
        
        existing_cols = [c for c in keep_cols if c in gdf.columns]
        gdf = gdf[existing_cols]
        
        return gdf
    except Exception as e:
        st.error(f"Ошибка загрузки данных: {e}. Пожалуйста, запустите main.py для генерации файла.")
        return None

def main():
    st.title("🗺️ Parametric Model of Tourism Framework (TOK T)")
    st.markdown("Интерактивная DSS-система предиктивного мастер-планирования региона")
    
    gdf = load_data()
    if gdf is None or gdf.empty:
        st.warning("Ожидание данных... Убедитесь, что 'data/processed/ucm_blocks_optimized.geojson' существует.")
        return
        
    # --- БОКОВАЯ ПАНЕЛЬ (SIDEBAR) ---
    st.sidebar.image("https://img.icons8.com/fluency/96/map-editing.png", width=80)
    st.sidebar.header("⚙️ Конфигурация Модели")
    
    scenario = st.sidebar.selectbox(
        "Сценарий развития:",
        ["Экоцентризм", "Историко-центризм", "Инфраструктурный"],
        help="Смена сценария меняет веса AHP и пересчитывает Target Land Use"
    )
    
    # Статистика по сценарию
    scenario_slug = scenario.replace("-", "_").replace(" ", "_")
    target_col = f"Target_LandUse_{scenario}"
    
    if target_col not in gdf.columns:
        st.error(f"Колонка {target_col} не найдена в данных. Пересчитайте модель.")
        return

    st.sidebar.markdown("---")
    st.sidebar.markdown("### Аналитика по региону")
    
    counts = gdf[target_col].value_counts()
    for cat, count in counts.items():
        st.sidebar.progress(int(count/len(gdf)*100), text=f"{cat}: {count} блоков")

    st.sidebar.markdown("### Легенда")
    for lu, color in LANDUSE_COLORS.items():
        st.sidebar.markdown(f'<div style="display:flex; align-items:center; margin-bottom:5px;"><div style="width:16px; height:16px; background-color:{color}; border-radius:3px; margin-right:8px;"></div><span style="font-size:14px;">{lu}</span></div>', unsafe_allow_html=True)

    # Подготовка данных для визуализации
    display_gdf = gdf.copy()
    display_gdf['Target_LU'] = display_gdf[target_col]
    display_gdf['Cap_Val'] = display_gdf[f"Capacity_{scenario}"].round(0).astype(int)
    
    # Извлечение S_ik для всплывашек
    # Категории: Хабы, Точки притяжения, Маршруты
    s_hubs = f"S_ik_{scenario_slug}_Опорные_центры_(Хабы)"
    s_points = f"S_ik_{scenario_slug}_Локальные_точки_притяжения"
    s_routes = f"S_ik_{scenario_slug}_Линейные_элементы_(Маршруты)"
    
    # Безопасное получение значений
    display_gdf['S_Hubs'] = display_gdf[s_hubs].round(3) if s_hubs in display_gdf.columns else 0.0
    display_gdf['S_Points'] = display_gdf[s_points].round(3) if s_points in display_gdf.columns else 0.0
    display_gdf['S_Routes'] = display_gdf[s_routes].round(3) if s_routes in display_gdf.columns else 0.0

    # Оценка приоритета
    display_gdf['Suitability'] = display_gdf.apply(lambda r: r.get(s_hubs, 0.0) if r['Target_LU'] == "Опорные центры (Хабы)" else (r.get(s_points, 0.0) if r['Target_LU'] == "Локальные точки притяжения" else r.get(s_routes, 0.0)), axis=1)
    
    conditions = [
        (display_gdf['Suitability'] >= 0.7),
        (display_gdf['Suitability'] >= 0.4),
        (display_gdf['Suitability'] < 0.4)
    ]
    choices = ['⭐ Высокий', '📈 Средний', '🐚 Низкий']
    display_gdf['Priority'] = np.select(conditions, choices, default='🐚 Низкий')

    # --- КАРТА ---
    bounds = display_gdf.total_bounds
    center_lat = (bounds[1] + bounds[3]) / 2
    center_lon = (bounds[0] + bounds[2]) / 2

    m = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=12,
        tiles="CartoDB dark_matter",
        control_scale=True
    )

    def style_func(feature):
        lu = feature['properties'].get('Target_LU', "Неизвестно")
        color = LANDUSE_COLORS.get(lu, "#808080")
        return {
            'fillColor': color,
            'color': '#ffffff',
            'weight': 0.8,
            'fillOpacity': 0.65
        }

    popup_fields = ['block_id', 'Target_LU', 'Priority', 'Cap_Val', 'S_Hubs', 'S_Points', 'S_Routes', 'dist_to_hubs']
    popup_aliases = ['🆔 ID:', '🎯 Тип ТОКТ:', '⚡ Приоритет:', '👥 Емкость (чел):', '🟣 S(Хаб):', '🟠 S(Точка):', '🟢 S(Маршрут):', '🛤️ ТПУ (м):']

    folium.GeoJson(
        display_gdf,
        style_function=style_func,
        highlight_function=lambda x: {'weight': 3, 'color': '#ffed00', 'fillOpacity': 0.9},
        tooltip=folium.GeoJsonTooltip(
            fields=popup_fields,
            aliases=popup_aliases,
            localize=True,
            sticky=False,
            labels=True,
            style="""
                background-color: #1a1c23;
                color: #ffffff;
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                font-size: 14px;
                padding: 12px;
                border: 2px solid #3e4451;
                border-radius: 8px;
                box-shadow: 3px 3px 10px rgba(0,0,0,0.5);
            """
        )
    ).add_to(m)

    st_folium(m, width="100%", height=750, returned_objects=[])
    
    st.info("💡 **Подсказка:** Наведите на блок, чтобы увидеть детальный расчет индексов пригодности $S_{ik}$ по всем альтернативам.")

if __name__ == "__main__":
    main()
