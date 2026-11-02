"""Product UI: implemented data operations and explicit future-feature placeholders."""

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.presentation.product_ui import clear_project, initialize_state

st.set_page_config(page_title="Туристический каркас", page_icon=str(ROOT / "assets" / "tourism-icon.svg"), layout="wide")
initialize_state()

page = st.navigation({
    "Проект": [st.Page("app_pages/territory.py", title="Территория", icon=":material/public:", default=True)],
    "Анализ": [
        st.Page("app_pages/framework.py", title="Каркас территории", icon=":material/map:"),
        st.Page("app_pages/restrictions.py", title="Территориальные ограничения", icon=":material/policy:"),
        st.Page("app_pages/accessibility.py", title="Доступность", icon=":material/route:"),
        st.Page("app_pages/services.py", title="Услуги и дефициты", icon=":material/store:"),
        st.Page("app_pages/trips.py", title="Поездки", icon=":material/travel_explore:"),
    ],
    "Решения": [
        st.Page("app_pages/interventions.py", title="Мероприятия", icon=":material/construction:"),
        st.Page("app_pages/scenarios.py", title="Сценарии", icon=":material/compare_arrows:"),
    ],
    "Данные": [st.Page("app_pages/sources.py", title="Источники и качество", icon=":material/database:")],
})

with st.sidebar:
    st.divider()
    if st.session_state.product_run_id:
        st.caption("Открытый проект")
        st.text(st.session_state.product_name.split(",")[0])
    else:
        st.caption("Территория пока не выбрана")
    with st.expander("Настройки хранения"):
        st.text_input("Хранилище проекта", key="product_store", on_change=clear_project)
        st.caption("Смена хранилища закрывает текущий проект. Снимки не удаляются.")
    st.caption("Исследовательский прототип · методика v1.2")

st.caption("ТУРИСТИЧЕСКИЙ КАРКАС · ПОДДЕРЖКА ТЕРРИТОРИАЛЬНЫХ РЕШЕНИЙ")
st.title(page.title)
notice = st.session_state.pop("product_notice", None)
if notice:
    st.success(notice)
page.run()
