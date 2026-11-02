from pathlib import Path

import streamlit as st

from src.presentation.operations import collect_territory
from src.presentation.product_ui import activate_run, catalog, require_view, show_operation_error

st.write("Выберите территорию исследования. Система собирает пространственные наблюдения и сохраняет проверяемый проект.")
new, saved = st.columns(2, gap="large")
with new, st.container(border=True):
    st.subheader("Новая территория / обновление")
    with st.form("product_collection"):
        query = st.text_input("Название территории", placeholder="Гатчина, Ленинградская область, Россия", key="product_query")
        with st.expander("Режим источников"):
            offline = st.checkbox("Только проверенный кэш — без сети", value=False, key="product_offline")
            st.caption("Обычный режим разрешает обращения к источникам. Офлайн-режим не продлевает сроки кэша.")
        st.caption(
            "Первый сбор может загрузить около 625 МиБ PBF и сохранить отдельную архивную копию. "
            "Окружение первичного сбора — 5 км; это не рассчитанная доступность."
        )
        submitted = st.form_submit_button("Собрать и открыть проект", width="stretch", key="product_collect_submit")
    if submitted:
        with st.status("Собираем данные территории…", expanded=True) as status:
            st.write("Выполняется действующий конвейер: граница, POI, дорожные линии, проверка необязательных источников и сохранение снимков.")
            operation = collect_territory(query, Path(st.session_state.product_store), offline=offline)
            if operation["ok"]:
                try:
                    activate_run(operation["result"]["run_manifest_id"])
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    operation = {**operation, "ok": False, "error": str(exc)}
            status.update(label="Проект сохранён" if operation["ok"] else "Сбор не завершён",
                          state="complete" if operation["ok"] else "error")
        if operation["ok"]:
            st.session_state.product_notice = "Пространственные наблюдения собраны, проект открыт. Неподтверждённые входы модели остаются неизвестными."
            st.rerun()
        else:
            show_operation_error(operation)

with saved, st.container(border=True):
    st.subheader("Продолжить работу")
    listing = catalog()
    if listing["errors"]:
        with st.expander(f"Непрочитанные записи: {len(listing['errors'])}"):
            for error in listing["errors"]:
                st.text(error)
    if listing["runs"]:
        names = list(dict.fromkeys(item["name"] for item in listing["runs"]))
        territory = st.selectbox("Сохранённая территория", names, format_func=lambda name: name.split(",")[0])
        runs = [item for item in listing["runs"] if item["name"] == territory]
        runs.sort(key=lambda item: (item["stage"] == "official_registry_enrichment", item["started_at"]), reverse=True)
        choices = {item["run_id"]: item for item in runs}
        selected = st.selectbox("Версия проекта", list(choices), format_func=lambda rid:
            f"{choices[rid]['started_at'][:19].replace('T', ' ')} UTC · "
            f"{'с официальными каталогами' if choices[rid]['stage'] == 'official_registry_enrichment' else 'первичный сбор'} · {rid[:8]}")
        if st.button("Открыть проект", width="stretch", key="product_open_saved"):
            try:
                activate_run(selected)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                st.error("Проект не прошёл проверку.")
                st.text(str(exc))
            else:
                st.session_state.product_notice = "Открыта сохранённая версия проекта. Источники автоматически не обновлялись."
                st.rerun()
    else:
        st.info("Сохранённых проектов пока нет. Начните со сбора территории — ручные файлы не нужны.")

if st.session_state.product_run_id:
    st.divider()
    view = require_view()
    cols = st.columns(3, border=True)
    cols[0].metric("Наблюдения POI", len(view["poi"]["features"]))
    cols[1].metric("Дорожные линии", len(view["network"]["features"]))
    cols[2].metric("Официальные каталоги", "подключены частично" if view["enrichment"] else "не подключены")
    st.caption("Число записей источника не равно числу уникальных заведений. Дороги ещё не являются маршрутизируемой сетью.")
    st.page_link("app_pages/framework.py", label="Открыть каркас территории", icon=":material/map:")
    st.page_link("app_pages/sources.py", label="Проверить источники / подключить официальные каталоги", icon=":material/database:")
