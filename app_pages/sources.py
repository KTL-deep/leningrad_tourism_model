import json
from pathlib import Path

import pandas as pd
import streamlit as st

from src.presentation.operations import enrich_territory
from src.presentation.product_ui import activate_run, require_view, show_operation_error
from src.presentation.views import report_markdown, summary_report

view = require_view()
st.write("Контролируйте происхождение, даты и полноту данных. Обновления запускаются только по вашему действию.")
statistics = view["collection"].get("statistics_discovery", {})
st.text(f"Официальная статистика: {statistics.get('status', 'неизвестно')} · причина: {statistics.get('reason') or 'не указана'}")
st.caption("Неудача необязательного источника не заменена нулями. Каталог ООПТ не равен юридическим границам или разрешению строительства.")

with st.container(border=True):
    st.subheader("Официальные каталоги")
    st.write("Подключить или обновить частичное обогащение ЕГРОКН / ООПТ и сопоставление с уже собранными POI.")
    offline = st.checkbox("Каталоги только из проверенного кэша — без сети", value=False)
    st.caption("Эта операция не обновляет исходный OSM. Для нового сбора используйте раздел «Территория».")
    if st.button("Подключить / обновить каталоги", width="stretch", key="product_enrich_submit"):
        source_id = view["enrichment"]["source_run_id"] if view["enrichment"] else view["run"]["run_id"]
        with st.status("Обновляем официальные каталоги…", expanded=True) as status:
            st.write("Выполняется действующий адаптерный путь с сохранением дочернего запуска и исходной версии проекта.")
            operation = enrich_territory(source_id, Path(st.session_state.product_store), offline=offline)
            if operation["ok"]:
                try:
                    activate_run(operation["result"]["run_manifest_id"])
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    operation = {**operation, "ok": False, "error": str(exc)}
            status.update(label="Новая версия сохранена" if operation["ok"] else "Обогащение не завершено",
                          state="complete" if operation["ok"] else "error")
        if operation["ok"]:
            st.session_state.product_notice = "Официальное обогащение сохранено новой версией. Правовые режимы и исходный OSM не объявлены обновлёнными."
            st.rerun()
        else:
            show_operation_error(operation)

st.dataframe(pd.DataFrame([{
    "Источник": s["source_id"], "URL": s["url"], "Получен (UTC)": s["retrieved_at"],
    "Полнота": s["completeness"], "Версия парсера": s["parser_version"],
    "SHA-256": s["content_hash"], "Snapshot ID": s["snapshot_id"],
} for s in view["sources"]]), hide_index=True, alt="Источники, даты и хэши снимков проекта")
st.warning("Проверка файлов не доказывает полноту услуг на местности. D/K, расписания, правовые режимы и осуществимость поездок пока не подтверждены.")
st.subheader("Экспорт текущего результата")
left, right = st.columns(2)
left.download_button("Отчёт по территории", report_markdown(view), file_name=f"territory-{view['run']['run_id']}.md",
                     mime="text/markdown", width="stretch")
right.download_button("Паспорт данных (JSON)", json.dumps(summary_report(view), ensure_ascii=False, indent=2),
                      file_name=f"territory-{view['run']['run_id']}.json", mime="application/json", width="stretch")
with st.expander("Техническая диагностика"):
    st.json(view["run"])
    if view["enrichment"]:
        st.json(view["enrichment"].get("sources", {}))
