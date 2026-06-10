import streamlit as st
import geopandas as gpd
import pandas as pd
import folium
from streamlit_folium import st_folium
import json
import numpy as np

# Конфигурация страницы (должна быть первым вызовом)
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

# Премиум-палитра цветов для элементов ТОКТ (не пересекается с дорожным графом)
LANDUSE_COLORS = {
    "Опорные центры (Хабы)": "#1e40af",      # Глубокий индиго-синий
    "Локальные точки притяжения": "#d97706", # Насыщенный янтарный/золотой
    "Линейные элементы (Маршруты)": "#059669", # Изумрудный зеленый
    "Неизвестно": "#6b7280"                   # Серый
}

@st.cache_data
def load_data():
    file_path = "data/processed/ucm_blocks_optimized.geojson"
    try:
        gdf = gpd.read_file(file_path)
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
        st.error(f"Ошибка загрузки данных: {e}. Пожалуйста, убедитесь, что файлы сгенерированы.")
        return None

@st.cache_data
def load_roads():
    file_path = "data/processed/drive_graph_edges.geojson"
    try:
        import os
        if not os.path.exists(file_path):
            return None
        roads = gpd.read_file(file_path)
        if roads.crs and roads.crs.to_epsg() != 4326:
            roads = roads.to_crs(epsg=4326)
        return roads
    except Exception:
        return None

def main():
    st.title("🗺️ Parametric Model of Tourism Framework (TOK T)")
    st.markdown("Интерактивная DSS-система предиктивного мастер-планирования региона")
    
    gdf = load_data()
    if gdf is None or gdf.empty:
        st.warning("Ожидание данных... Убедитесь, что 'data/processed/ucm_blocks_optimized.geojson' существует.")
        return
        
    # --- РАСЧЕТ И АНАЛИЗ СЦЕНАРИЕВ (DSS) ---
    scenarios = ["Экоцентризм", "Историко-центризм", "Инфраструктурный"]
    sc_stats = {}
    best_scenario = None
    max_score = -1.0

    for sc in scenarios:
        sc_slug = sc.replace("-", "_").replace(" ", "_")
        target_col = f"Target_LandUse_{sc}"
        cap_col = f"Capacity_{sc}"
        
        if target_col in gdf.columns and cap_col in gdf.columns:
            # Расчет пригодности выбранного типа застройки для каждого блока в данном сценарии
            def get_block_suitability(row):
                lu = row[target_col]
                if lu == "Опорные центры (Хабы)":
                    col = f"S_ik_{sc_slug}_Опорные_центры_(Хабы)"
                elif lu == "Локальные точки притяжения":
                    col = f"S_ik_{sc_slug}_Локальные_точки_притяжения"
                elif lu == "Линейные элементы (Маршруты)":
                    col = f"S_ik_{sc_slug}_Линейные_элементы_(Маршруты)"
                else:
                    return 0.0
                return row.get(col, 0.0)
            
            suits = gdf.apply(get_block_suitability, axis=1)
            mean_suit = suits.mean()
            total_cap = int(np.floor(gdf[cap_col].fillna(0.0)).sum())
            
            # Интегральная эффективность сценария (Соответствие пригодности * Общая емкость)
            score = mean_suit * total_cap
            sc_stats[sc] = {
                "mean_suit": mean_suit,
                "total_cap": total_cap,
                "score": score
            }
            if score > max_score:
                max_score = score
                best_scenario = sc

    # --- ИНТЕРФЕЙС РЕКОМЕНДАЦИИ СЦЕНАРИЯ ---
    st.markdown("### Лучший сценарий развития территорий")
    
    col1, col2, col3 = st.columns(3)
    for i, sc_name in enumerate(scenarios):
        if sc_name not in sc_stats:
            continue
        stats = sc_stats[sc_name]
        is_best = (sc_name == best_scenario)
        badge = "🏆 РЕКОМЕНДУЕМЫЙ" if is_best else "⏳ Альтернативный"
        bg_color = "rgba(16, 185, 129, 0.15)" if is_best else "rgba(107, 114, 128, 0.05)"
        border_color = "#10b981" if is_best else "#4b5563"
        text_color = "#34d399" if is_best else "#9ca3af"
        
        with [col1, col2, col3][i]:
            st.markdown(f"""
                <div style="background-color: {bg_color}; border: 2px solid {border_color}; border-radius: 8px; padding: 15px; text-align: center; height: 100%;">
                    <span style="background-color: {border_color}; color: #ffffff; padding: 3px 8px; border-radius: 4px; font-size: 11px; font-weight: bold; text-transform: uppercase;">{badge}</span>
                    <h4 style="margin: 10px 0 5px 0; color: #f0f2f6; font-size: 18px;">{sc_name}</h4>
                    <div style="font-size: 14px; color: {text_color}; line-height: 1.6; margin-top: 8px;">
                        <b>Интегральный балл:</b> {stats['score']:.2f}<br>
                        <b>Ср. пригодность S<sub>ik</sub>:</b> {stats['mean_suit']:.2f}<br>
                        <b>Емкость размещения:</b> {stats['total_cap']:,} чел.
                    </div>
                </div>
            """, unsafe_allow_html=True)
            
    # --- СПРАВОЧНИК ПО ИНДЕКСАМ И ГЛОССАРИЮ (Points 2 & 3) ---
    st.markdown("<div style='margin-top: 15px;'></div>", unsafe_allow_html=True)
    with st.expander("📚 Справочник: Индексы пригодности и глоссарий понятий ТОКТ"):
        tab1, tab2 = st.tabs(["📊 Индексы пригодности (S_ik)", "🧩 Основные понятия ТОКТ"])
        
        with tab1:
            st.markdown("""
            **Индекс пригодности ($S_{ik}$)** — это безразмерный показатель от **0.0** (минимальный потенциал) до **1.0** (максимальный потенциал), рассчитываемый по методу анализа иерархий Томаса Саати на основе физических и пространственных характеристик территории.
            
            *   **🟣 Индекс Опорного центра $S(\text{Хаб})$**: Оценивает пригодность блока для создания гостинично-ресторанных комплексов и ТПУ. Учитывает концентрацию мест питания, объектов проживания и близость к остановкам транспорта.
            *   **🟠 Индекс Точки притяжения $S(\text{Точка})$**: Оценивает концентрацию памятников культуры, музеев, усадеб и других объектов культурного наследия (ОКН).
            *   **🟢 Индекс Маршрута $S(\text{Маршрут})$**: Оценивает экологический потенциал территории. Зависит от доли лесов, плотности водоемов и удаленности от загрязняющих транспортных артерий.
            """)
            
        with tab2:
            st.markdown("""
            **ТОКТ (Туристско-рекреационный каркас территории)** — пространственная структура развития региона, разделенная на следующие типы использования (Target Land Use):
            
            *   **Опорные центры (Хабы)** — главные узлы приема туристов, концентрирующие инфраструктуру размещения (отели, кемпинги), питания (рестораны) и транспортной доступности.
            *   **Локальные точки притяжения** — культурные достопримечательности, парки, ОКН, требующие кратковременного посещения.
            *   **Линейные элементы (Маршруты)** — велопешеходные и экологические связи, проходящие через природные ландшафты, а также экотропы.
            *   **Целевое использование (Target Land Use)** — предписанный алгоритмом Simulated Annealing тип развития, обеспечивающий экологический баланс и синергию.
            *   **Приоритет развития** — ранжирование блоков (Высокий / Средний / Низкий) по квантилям (процентилям) распределения баллов пригодности $S_{ik}$. Позволяет инвесторам определить приоритетность освоения участков.
            """)

    st.markdown("---")

    # --- БОКОВАЯ ПАНЕЛЬ (SIDEBAR) ---
    st.sidebar.image("https://img.icons8.com/fluency/96/map-editing.png", width=80)
    st.sidebar.header("⚙️ Конфигурация Модели")
    
    scenario = st.sidebar.selectbox(
        "Сценарий развития:",
        scenarios,
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

    st.sidebar.markdown("### Визуализация")
    show_roads = st.sidebar.toggle("🛣️ Транспортный граф (доступность)", value=False, help="Отобразить дорожную сеть, использованную для расчета матрицы доступности")
    
    color_mode = st.sidebar.selectbox(
        "Режим раскраски блоков:",
        [
            "Целевое использование (ТОКТ)", 
            "Приоритет развития (Теплокарта)", 
            "Емкость размещения",
            "Доля лесов (%)",
            "Расстояние до ТПУ (м)"
        ],
        help="Выберите показатель, по которому будут раскрашены городские блоки на карте"
    )

    # Стабильная легенда для графа дорог в боковой панели (Point 5)
    if show_roads:
        roads_gdf = load_roads()
        if roads_gdf is not None and not roads_gdf.empty and 'time_min' in roads_gdf.columns:
            st.sidebar.markdown("#### ⏱️ Легенда: Доступность дорог")
            st.sidebar.markdown(f"Время проезда в минутах (от **{roads_gdf['time_min'].min():.2f}** до **{roads_gdf['time_min'].max():.2f}**):")
            st.sidebar.markdown("""
                <div style="background: linear-gradient(to right, #f472b6, #a855f7, #4c1d95); height: 15px; border-radius: 5px; margin-bottom: 5px;"></div>
                <div style="display: flex; justify-content: space-between; font-size: 11px; color: #a1a1aa; margin-bottom: 15px;">
                    <span>⚡ Быстро (Розовый)</span>
                    <span>🚗 Средне (Фиолетовый)</span>
                    <span>🛑 Медленно (Темный)</span>
                </div>
            """, unsafe_allow_html=True)
        else:
            st.sidebar.warning("Файл графа дорог не найден. Сгенерируйте data/processed/drive_graph_edges.geojson через main.py.")

    # Вычисляем min/max для динамических легенд (чтобы вывести легенды в боковой панели)
    cap_vals = gdf[f"Capacity_{scenario}"].round(0).astype(int) if f"Capacity_{scenario}" in gdf.columns else pd.Series([0])
    cap_min, cap_max = int(cap_vals.min()), int(cap_vals.max())
    if cap_max == cap_min: cap_max = cap_min + 1
    
    forest_vals = gdf['forest_share'].fillna(0.0) * 100.0 if 'forest_share' in gdf.columns else pd.Series([0.0])
    forest_min, forest_max = float(forest_vals.min()), float(forest_vals.max())
    if forest_max == forest_min: forest_max = forest_min + 1.0
    
    dist_vals = gdf['dist_to_hubs'].fillna(0.0) if 'dist_to_hubs' in gdf.columns else pd.Series([0.0])
    dist_min, dist_max = float(dist_vals.min()), float(dist_vals.max())
    if dist_max == dist_min: dist_max = dist_min + 1.0

    st.sidebar.markdown("### Легенда карты")
    if color_mode == "Целевое использование (ТОКТ)":
        for lu, color in LANDUSE_COLORS.items():
            st.sidebar.markdown(f'<div style="display:flex; align-items:center; margin-bottom:5px;"><div style="width:16px; height:16px; background-color:{color}; border-radius:3px; margin-right:8px;"></div><span style="font-size:14px;">{lu}</span></div>', unsafe_allow_html=True)
            
    elif color_mode == "Приоритет развития (Теплокарта)":
        st.sidebar.markdown('<div style="display:flex; align-items:center; margin-bottom:5px;"><div style="width:16px; height:16px; background-color:#e11d48; border-radius:3px; margin-right:8px;"></div><span style="font-size:14px;">⭐ Высокий (Топ-25% пригодности)</span></div>', unsafe_allow_html=True)
        st.sidebar.markdown('<div style="display:flex; align-items:center; margin-bottom:5px;"><div style="width:16px; height:16px; background-color:#f59e0b; border-radius:3px; margin-right:8px;"></div><span style="font-size:14px;">📈 Средний (35%)</span></div>', unsafe_allow_html=True)
        st.sidebar.markdown('<div style="display:flex; align-items:center; margin-bottom:5px;"><div style="width:16px; height:16px; background-color:#475569; border-radius:3px; margin-right:8px;"></div><span style="font-size:14px;">🐚 Низкий (40%)</span></div>', unsafe_allow_html=True)
        
    elif color_mode == "Емкость размещения":
        st.sidebar.markdown(f"Количество человек (от **{cap_min}** до **{cap_max}**):")
        st.sidebar.markdown(f"""
            <div style="background: linear-gradient(to right, #1e293b, #6366f1, #a855f7, #ec4899, #f59e0b); height: 15px; border-radius: 5px; margin-bottom: 5px;"></div>
            <div style="display: flex; justify-content: space-between; font-size: 11px; color: #a1a1aa; margin-bottom: 15px;">
                <span>Мало ({cap_min})</span>
                <span>Много ({cap_max})</span>
            </div>
        """, unsafe_allow_html=True)
        
    elif color_mode == "Доля лесов (%)":
        st.sidebar.markdown(f"Процент лесопокрытия (от **{forest_min:.2f}%** до **{forest_max:.2f}%**):")
        st.sidebar.markdown(f"""
            <div style="background: linear-gradient(to right, #1e293b, #10b981, #064e3b); height: 15px; border-radius: 5px; margin-bottom: 5px;"></div>
            <div style="display: flex; justify-content: space-between; font-size: 11px; color: #a1a1aa; margin-bottom: 15px;">
                <span>0.00%</span>
                <span>{forest_max:.2f}%</span>
            </div>
        """, unsafe_allow_html=True)
        
    elif color_mode == "Расстояние до ТПУ (м)":
        st.sidebar.markdown(f"Дистанция в метрах (от **{dist_min:.2f}** до **{dist_max:.2f}** м):")
        st.sidebar.markdown(f"""
            <div style="background: linear-gradient(to right, #06b6d4, #3b82f6, #1e293b); height: 15px; border-radius: 5px; margin-bottom: 5px;"></div>
            <div style="display: flex; justify-content: space-between; font-size: 11px; color: #a1a1aa; margin-bottom: 15px;">
                <span>Близко (0.00 м)</span>
                <span>Далеко ({dist_max:.2f} м)</span>
            </div>
        """, unsafe_allow_html=True)

    # Подготовка данных для визуализации
    display_gdf = gdf.copy()
    display_gdf['Target_LU'] = display_gdf[target_col]
    display_gdf['Cap_Val'] = np.floor(display_gdf[f"Capacity_{scenario}"].fillna(0.0)).astype(int)
    
    # Извлечение S_ik для всплывашек
    s_hubs = f"S_ik_{scenario_slug}_Опорные_центры_(Хабы)"
    s_points = f"S_ik_{scenario_slug}_Локальные_точки_притяжения"
    s_routes = f"S_ik_{scenario_slug}_Линейные_элементы_(Маршруты)"
    
    # Безопасное получение значений
    display_gdf['S_Hubs'] = display_gdf[s_hubs].round(2) if s_hubs in display_gdf.columns else 0.0
    display_gdf['S_Points'] = display_gdf[s_points].round(2) if s_points in display_gdf.columns else 0.0
    display_gdf['S_Routes'] = display_gdf[s_routes].round(2) if s_routes in display_gdf.columns else 0.0
    display_gdf['dist_to_hubs'] = display_gdf['dist_to_hubs'].round(2) if 'dist_to_hubs' in display_gdf.columns else 0.0

    # Вычисление пригодности текущего блока под назначенное использование
    display_gdf['Suitability'] = display_gdf.apply(lambda r: r.get(s_hubs, 0.0) if r['Target_LU'] == "Опорные центры (Хабы)" else (r.get(s_points, 0.0) if r['Target_LU'] == "Локальные точки притяжения" else r.get(s_routes, 0.0)), axis=1)
    
    # Расчет квантилей для исправления ошибки занижения приоритетов (Point 1)
    q_high = display_gdf['Suitability'].quantile(0.75)
    q_medium = display_gdf['Suitability'].quantile(0.40)
    
    # Корректный фоллбек, если все значения нулевые
    if q_high == 0.0:
        q_high = 0.7
        q_medium = 0.4
        
    conditions = [
        (display_gdf['Suitability'] >= q_high),
        (display_gdf['Suitability'] >= q_medium),
        (display_gdf['Suitability'] < q_medium)
    ]
    choices = ['⭐ Высокий', '📈 Средний', '🐚 Низкий']
    display_gdf['Priority'] = np.select(conditions, choices, default='🐚 Низкий')

    # Инициализация цветовых шкал (Colormaps)
    import branca.colormap as cm
    cap_colormap = cm.LinearColormap(
        colors=['#1e293b', '#6366f1', '#a855f7', '#ec4899', '#f59e0b'],
        vmin=cap_min,
        vmax=cap_max
    )
    forest_colormap = cm.LinearColormap(
        colors=['#1e293b', '#10b981', '#064e3b'],
        vmin=forest_min,
        vmax=forest_max
    )
    dist_colormap = cm.LinearColormap(
        colors=['#06b6d4', '#3b82f6', '#1e293b'],
        vmin=dist_min,
        vmax=dist_max
    )

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

    # Выбор динамического стиля заливки в зависимости от выбранного режима раскраски
    if color_mode == "Целевое использование (ТОКТ)":
        def style_func(feature):
            lu = feature['properties'].get('Target_LU', "Неизвестно")
            color = LANDUSE_COLORS.get(lu, "#6b7280")
            return {
                'fillColor': color,
                'color': '#ffffff',
                'weight': 0.6,
                'fillOpacity': 0.65
            }
    elif color_mode == "Приоритет развития (Теплокарта)":
        PRIORITY_COLORS = {
            "⭐ Высокий": "#e11d48",
            "📈 Средний": "#f59e0b",
            "🐚 Низкий": "#475569"
        }
        def style_func(feature):
            pri = feature['properties'].get('Priority', "🐚 Низкий")
            color = PRIORITY_COLORS.get(pri, "#475569")
            return {
                'fillColor': color,
                'color': '#ffffff',
                'weight': 0.6,
                'fillOpacity': 0.70
            }
    elif color_mode == "Емкость размещения":
        def style_func(feature):
            val = feature['properties'].get('Cap_Val', 0)
            color = cap_colormap(val)
            return {
                'fillColor': color,
                'color': '#ffffff',
                'weight': 0.6,
                'fillOpacity': 0.70
            }
    elif color_mode == "Доля лесов (%)":
        def style_func(feature):
            val = feature['properties'].get('forest_share', 0.0) * 100.0
            color = forest_colormap(val)
            return {
                'fillColor': color,
                'color': '#ffffff',
                'weight': 0.6,
                'fillOpacity': 0.70
            }
    elif color_mode == "Расстояние до ТПУ (м)":
        def style_func(feature):
            val = feature['properties'].get('dist_to_hubs', 0.0)
            color = dist_colormap(val)
            return {
                'fillColor': color,
                'color': '#ffffff',
                'weight': 0.6,
                'fillOpacity': 0.70
            }

    # 1. Отображение блоков (добавляется снизу)
    popup_fields = ['block_id', 'Target_LU', 'Priority', 'Cap_Val', 'S_Hubs', 'S_Points', 'S_Routes', 'dist_to_hubs']
    popup_aliases = ['🆔 ID:', '🎯 Тип ТОКТ:', '⚡ Приоритет:', '👥 Емкость (чел):', '🟣 S(Хаб):', '🟠 S(Точка):', '🟢 S(Маршрут):', '🛤️ ТПУ (м):']

    folium.GeoJson(
        display_gdf,
        style_function=style_func,
        highlight_function=lambda x: {'weight': 3, 'color': '#ffed00', 'fillOpacity': 0.85},
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

    # 2. Отображение дорожной сети (добавляется сверху, поверх блоков)
    if show_roads and 'roads_gdf' in locals() or show_roads:
        roads_gdf = load_roads()
        if roads_gdf is not None and not roads_gdf.empty:
            if 'time_min' in roads_gdf.columns:
                # Градиент от Розового через Фиолетовый к глубокому Индиго
                colormap = cm.LinearColormap(
                    colors=['#f472b6', '#a855f7', '#4c1d95'], 
                    vmin=roads_gdf['time_min'].min(), 
                    vmax=roads_gdf['time_min'].max()
                )
                
                def road_style(feature):
                    val = feature['properties'].get('time_min', 0)
                    return {'color': colormap(val), 'weight': 2.5, 'opacity': 0.95}
            else:
                def road_style(feature):
                    return {'color': '#a855f7', 'weight': 2.0, 'opacity': 0.8}
            
            folium.GeoJson(
                roads_gdf,
                style_function=road_style,
                name="Транспортный граф",
                interactive=False # События мыши проходят сквозь дороги на лежащие под ними блоки
            ).add_to(m)

    st_folium(m, width="100%", height=750, returned_objects=[])
    
    st.info("💡 **Подсказка:** Наведите курсор на интересующий городской блок, чтобы изучить детальные индексы пригодности $S_{ik}$ по всем альтернативам развития.")

if __name__ == "__main__":
    main()
