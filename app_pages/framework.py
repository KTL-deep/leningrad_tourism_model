import pandas as pd
import streamlit as st

from src.presentation.product_ui import require_view
from src.presentation.views import map_spec, matching_rows, poi_rows

view = require_view()
st.caption("Существующие наблюдения: граница, окружение сбора, POI и исходные дороги. Типы и категории не подтверждают функции туристических узлов.")
rows = poi_rows(view["poi"])
mode = st.segmented_control("Слой работы", ["Карта и объекты", "Сопоставление ЕГРОКН"], default="Карта и объекты")
if mode == "Карта и объекты":
    filters, search = st.columns(2)
    all_categories = sorted({cat.strip() for row in rows for cat in row["categories"].split(",")})
    categories = filters.multiselect("Категории наблюдений", all_categories, default=all_categories)
    query = search.text_input("Поиск объекта по названию или OSM ID", key="product_poi_query").strip().casefold()
    filtered = [row for row in rows if any(cat.strip() in categories for cat in row["categories"].split(","))
                and (not query or query in f"{row['name']} {row['id']}".casefold())]
    roads = st.checkbox("Исходные дорожные линии", value=True)
    objects = {row["id"]: row for row in filtered}
    selected = st.selectbox("Выделить объект", [None, *objects], key="product_poi_focus",
                            format_func=lambda pid: "Не выделять" if pid is None else f"{objects[pid]['name']} · {pid}")
    st.vega_lite_chart(spec=map_spec(view["collection"], view["network"], filtered, roads, selected),
                       width="stretch", theme=None, alt="Граница территории, область сбора, исходные дороги и наблюдения POI")
    st.caption(f"Показано {len(filtered)} из {len(rows)} POI с пригодной точечной геометрией. Тёмно-синий контур — граница, серый — область первичного сбора. © OpenStreetMap contributors · ODbL.")
    st.info("Дорожные линии не являются готовыми маршрутами. Центры way/relation приблизительны и не подтверждают положение входа или публичный доступ.")
    if selected:
        obj = objects[selected]
        st.text(f"{obj['name']} · {obj['id']}")
        st.text(f"Точность: {obj['precision']} · публичный доступ: {obj['access']} · мощность: {obj['capacity']}")
        st.json(obj["tags"], expanded=False)
    st.dataframe(pd.DataFrame([{k: v for k, v in row.items() if k != "tags"} for row in filtered]), hide_index=True,
                 column_config={"id": "OSM ID", "name": "Название", "categories": "Категории",
                                "longitude": "Долгота", "latitude": "Широта", "precision": "Точность",
                                "access": "Публичный доступ", "capacity": "Мощность услуги"},
                 alt="Исходные наблюдения выбранных категорий")
else:
    matching = view["matching"]
    if matching is None:
        st.info("ЕГРОКН для этой версии не сопоставлен. Подключите официальные каталоги в разделе «Источники и качество».")
    else:
        st.warning("Сопоставления рекомендательные. ID-связи не доказывают физическую идентичность, кандидаты требуют проверки. Автоматического слияния нет.")
        channel = st.selectbox("Канал", ["Все", "ID-связь", "Кандидат", "Исключён из очереди"])
        query = st.text_input("Поиск пары").strip().casefold()
        pairs = [pair for pair in matching_rows(matching) if (channel == "Все" or pair["channel"] == channel)
                 and (not query or query in " ".join(str(value) for value in pair.values()).casefold())]
        st.dataframe(pd.DataFrame(pairs), hide_index=True,
                     column_config={"channel": "Канал", "poi_id": "OSM ID", "source_record_id": "ID реестра",
                                    "osm_name": "Название OSM", "official_name": "Название реестра",
                                    "status": "Статус", "distance_m": "Расстояние, м", "reason": "Причина", "warnings": "Оговорки"},
                     alt="ID-связи, кандидаты и исключённые пары ЕГРОКН / OSM")
        st.caption("Пустое расстояние означает неизвестность, не ноль метров. Правовая геометрия и допустимость действий этим не установлены.")
