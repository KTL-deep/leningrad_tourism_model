"""Shared product state and views; no implicit source requests on page navigation."""

import os
from pathlib import Path

import streamlit as st

from src.presentation.results import list_runs, load_run_view


ROOT = Path(__file__).resolve().parents[2]


def initialize_state():
    pilot = ROOT / "data" / "analysis" / "e03_acceptance_20261024"
    default = os.environ.get("TOURISM_PRODUCT_STORE", os.environ.get(
        "TOURISM_PRESENTATION_STORE", str(pilot if pilot.exists() else ROOT / "data" / "analysis")))
    for key, value in (("product_store", default), ("product_run_id", None),
                       ("product_name", ""), ("product_notice", None)):
        if key not in st.session_state:
            st.session_state[key] = value


def clear_project():
    st.session_state.product_run_id = None
    st.session_state.product_name = ""
    st.session_state.product_notice = None


def activate_run(run_id):
    # Verify before changing selection; failed operations cannot replace a valid project.
    view = load_run_view(Path(st.session_state.product_store), run_id)
    st.session_state.product_run_id = run_id
    st.session_state.product_name = view["collection"]["study_area"]["name"]
    return view


def catalog():
    # Metadata only. Selected results are never cached across reruns.
    try:
        return list_runs(Path(st.session_state.product_store))
    except (OSError, ValueError, TypeError) as exc:
        st.error("Не удалось прочитать хранилище проектов.")
        st.text(str(exc))
        return {"runs": [], "errors": []}


def require_view():
    if not st.session_state.product_run_id:
        st.info("Сначала выберите территорию: соберите данные или откройте сохранённый проект.")
        st.page_link("app_pages/territory.py", label="Выбрать территорию", icon=":material/public:")
        st.stop()
    try:
        with st.spinner("Проверяем снимки проекта…"):
            view = load_run_view(Path(st.session_state.product_store), st.session_state.product_run_id)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        st.error("Данные проекта не прошли проверку. Расчёты и карта не показаны.")
        st.text(str(exc))
        st.page_link("app_pages/territory.py", label="Вернуться к выбору территории")
        st.stop()
    st.subheader(view["collection"]["study_area"]["name"].split(",")[0])
    st.caption(
        f"Сохранённый результат: {view['run']['started_at'][:19].replace('T', ' ')} UTC · "
        f"статус данных: {view['run']['status']}. Открытие проекта не обновляет источники."
    )
    return view


def show_operation_error(operation):
    st.error("Операция не завершена. Ранее открытый проект сохранён."
             if st.session_state.product_run_id else "Операция не завершена. Новый проект не открыт.")
    st.text(operation.get("error") or "Неизвестная причина")
    if operation.get("result"):
        if operation["result"].get("error"):
            st.text(f"Причина конвейера: {operation['result']['error']}")
        st.json(operation["result"], expanded=False)
    with st.expander("Диагностика операции"):
        st.json({key: operation.get(key) for key in ("returncode", "stdout_log", "stderr_log")})


def placeholder(description, planned, button):
    st.warning("В разработке")
    st.write(description)
    if st.session_state.product_name:
        st.caption(f"Проект: {st.session_state.product_name.split(',')[0]}")
    with st.container(border=True):
        st.subheader("Возможности раздела")
        for item in planned:
            st.markdown(f"- {item}")
        st.button(button, disabled=True)
    st.caption("Модуль ещё не подключён. Демонстрационные результаты и неподтверждённые расчёты не подставляются.")
