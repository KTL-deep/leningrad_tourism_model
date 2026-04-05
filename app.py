import streamlit as st
import geopandas as gpd
import folium
from streamlit_folium import st_folium
import branca.colormap as cm
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

@st.cache_data
def load_data():
    file_path = "data/processed/ucm_blocks_with_attractiveness.geojson"
    try:
        gdf = gpd.read_file(file_path)
        # Принудительно приводим к EPSG:4326 для Folium
        if gdf.crs and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)
        
        # Убираем возможные артефакты, где атрибуты могут быть None
        gdf['dominant_landuse'] = gdf['dominant_landuse'].fillna("Неизвестно")
        gdf['attractiveness_score'] = gdf['attractiveness_score'].fillna(0.0)
        gdf['attractiveness_rank'] = gdf['attractiveness_rank'].fillna(9999).astype(int)
        
        # Форматирование чисел для красивого Tooltip
        gdf['forest_share'] = (gdf['forest_share'] * 100).round(1).astype(str) + "%"
        gdf['attractiveness_score'] = gdf['attractiveness_score'].round(4)
        
        return gdf
    except Exception as e:
        st.error(f"Ошибка загрузки данных: {e}. Пожалуйста, запустите main.py для генерации файла.")
        return None

def main():
    st.title("🗺️ Интерактивная модель: Туристический потенциал территорий")
    st.markdown("Предиктивная визуализация (Suitability Analysis) городских блоков по методу AHP (Шаг 3).")
    
    gdf = load_data()
    if gdf is None or gdf.empty:
        return
        
    # --- БОКОВАЯ ПАНЕЛЬ (SIDEBAR) ---
    st.sidebar.header("⚙️ Параметры отображения")
    
    total_blocks = len(gdf)
    st.sidebar.metric("Всего полигонов (блоков):", total_blocks)
    
    st.sidebar.markdown("### Фильтрация блоков")
    top_n = st.sidebar.slider(
        "Отображать только ТОП-N лучших блоков:",
        min_value=1,
        max_value=total_blocks,
        value=total_blocks,
        step=1
    )
    
    # Фильтруем данные: сортируем по рангу
    filtered_gdf = gdf.sort_values(by="attractiveness_rank", ascending=True).head(top_n)
    
    st.sidebar.markdown("---")
    st.sidebar.info(
        "**Цветовая палитра: YlOrRd**\n\n"
        "🟡 Желтый — низкий потенциал\n\n"
        "🔴 Красный — высокий приоритет для инвестиций"
    )

    # --- КАРТА FOLIUM ---
    bounds = filtered_gdf.total_bounds
    center_lat = (bounds[1] + bounds[3]) / 2
    center_lon = (bounds[0] + bounds[2]) / 2

    # Используем премиальную темную подложку
    m = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=14,
        tiles="CartoDB dark_matter"
    )

    min_score = filtered_gdf['attractiveness_score'].min()
    max_score = filtered_gdf['attractiveness_score'].max()
    
    # Цветовая карта от желтого к красному (YlOrRd_09)
    # Если все score одинаковые (бывает при сбоях), даем дефолтные края
    if min_score == max_score:
        min_score, max_score = 0.0, 1.0
        
    colormap = cm.linear.YlOrRd_09.scale(min_score, max_score)
    colormap.caption = 'Туристическая привлекательность (AHP Score)'

    def style_function(feature):
        score = feature['properties'].get('attractiveness_score', 0)
        return {
            'fillColor': colormap(score),
            'color': '#ffffff',  # белые границы
            'weight': 0.5,
            'fillOpacity': 0.75
        }

    def highlight_function(feature):
        return {
            'fillColor': '#00ffff',  # неоново-синий при наведении
            'color': '#00ffff',
            'weight': 2,
            'fillOpacity': 0.9
        }

    folium.GeoJson(
        filtered_gdf,
        style_function=style_function,
        highlight_function=highlight_function,
        tooltip=folium.GeoJsonTooltip(
            fields=[
                'block_id', 
                'dominant_landuse', 
                'attractiveness_rank', 
                'attractiveness_score', 
                'accommodation_count', 
                'food_count', 
                'transport_count', 
                'forest_share'
            ],
            aliases=[
                '🆔 ID Блока:', 
                '🏙️ Землепользование:', 
                '🏆 Рейтинг (Rank):', 
                '📊 Балл (Score):', 
                '🛏️ Места размещения:', 
                '🍽️ Точки питания:', 
                '🚌 Остановки/Станции:', 
                '🌲 Доля леса:'
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

    colormap.add_to(m)

    # Отрисовка в веб-интерфейсе
    # Конфигурируем высоту и отключаем возвращение объектов (returned_objects=[]) для производительности
    st_folium(m, width="100%", height=700, returned_objects=[])

if __name__ == "__main__":
    main()
