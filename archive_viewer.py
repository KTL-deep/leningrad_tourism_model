"""Technical archive viewer; the product entry point is current_app.py."""

import json
import os
from pathlib import Path
import sys

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.presentation.results import list_runs, load_run_view
from src.presentation.views import map_spec, matching_rows, poi_rows, report_markdown, summary_report


@st.cache_data(ttl=30, max_entries=8)
def run_catalog(path):
    return list_runs(Path(path))


def verified_view(path, run_id):
    # Deliberately not cached: archive damage must fail closed on the next rerun.
    return load_run_view(Path(path), run_id)


st.set_page_config(
    page_title="Туристический каркас | текущая разработка",
    layout="wide",
)

st.caption("ЛЕНИНГРАДСКАЯ ОБЛАСТЬ · ПИЛОТ ПАВЛОВСК — ГАТЧИНА · МЕТОДИКА v1.2")
st.title("Туристический каркас")
st.markdown("От территории — к **проверяемым данным** и обоснованным решениям.")
st.info(
    "Текущая стадия: первичный сбор E03 и частичное обогащение E04. "
    "Показ сохранённого архива без обращения к источникам, не свежий сбор. "
    "Маршруты, количественные дефициты и рекомендации ещё не рассчитаны."
)

pilot_store = ROOT / "data" / "analysis" / "e03_acceptance_20261024"
default_store = os.environ.get(
    "TOURISM_PRESENTATION_STORE", str(pilot_store if pilot_store.exists() else ROOT / "data" / "analysis")
)
with st.sidebar:
    st.header("Область исследования")
    with st.expander("Хранилище и обновление экрана"):
        store_path = st.text_input("Каталог снимков", default_store)
        if st.button("Перечитать архив", width="stretch"):
            run_catalog.clear()
            st.rerun()
        st.caption("Чтение архива не обновляет исходные данные. Сетевых запросов нет.")

try:
    catalog = run_catalog(store_path)
except (OSError, ValueError, TypeError) as exc:
    st.error("Не удалось прочитать каталог сохранённых запусков.")
    st.text(str(exc))
    st.stop()

if catalog["errors"]:
    with st.expander(f"Не прочитаны записи архива: {len(catalog['errors'])}"):
        for error in catalog["errors"]:
            st.text(error)

if not catalog["runs"]:
    st.warning("В этом хранилище нет сохранённых результатов E03/E04. Данные не подставляются.")
    st.markdown("Для первичного сбора достаточно названия территории:")
    st.code('.\\.venv-app\\Scripts\\python.exe -X utf8 -m scripts.collect_territory "Гатчина, Ленинградская область, Россия"', language="powershell")
    st.caption("Сбор выполняется отдельно и может загружать крупный PBF. Затем выберите его хранилище слева.")
    st.stop()

names = list(dict.fromkeys(run["name"] for run in catalog["runs"]))
with st.sidebar:
    territory = st.selectbox("Территория", names)
    territory_runs = [run for run in catalog["runs"] if run["name"] == territory]
    # Prefer the most recent enrichment, without making an old archive look fresh.
    enriched = [run for run in territory_runs if run["stage"] == "official_registry_enrichment"]
    ordered = enriched + [run for run in territory_runs if run not in enriched]
    by_id = {run["run_id"]: run for run in ordered}
    selected_run = st.selectbox(
        "Сохранённый запуск", list(by_id),
        format_func=lambda rid: (
            f"{'E04' if by_id[rid]['stage'] == 'official_registry_enrichment' else 'E03'} · "
            f"{by_id[rid]['started_at'][:19].replace('T', ' ')} · {rid[:8]}"
        ),
    )
    st.caption("Время запуска указано в UTC. Список — каталог метаданных; выбранный запуск проверяется по хэшам.")

try:
    with st.spinner("Проверяем манифест, ссылки и хэши архивных данных…"):
        view = verified_view(store_path, selected_run)
except (OSError, ValueError, TypeError, KeyError) as exc:
    st.error("Выбранный запуск не прошёл проверку. Карта и показатели не показаны.")
    st.text(str(exc))
    st.caption("Восстановите архив либо выберите другой запуск. Изменяемые GeoJSON-экспорты не используются как запасной источник.")
    st.stop()

collection = view["collection"]
matching = view["matching"]
summary = summary_report(view)
rows = poi_rows(view["poi"])
pairs = matching_rows(matching)
st.subheader(collection["study_area"]["name"])
st.caption(f"RunManifest: {selected_run} · статус данных: {view['run']['status']} · просмотр проверенного архива")

metrics = st.columns(4, border=True)
metrics[0].metric("Наблюдения POI", f"{len(view['poi']['features']):,}".replace(",", " "))
metrics[1].metric("Дорожные линии", f"{len(view['network']['features']):,}".replace(",", " "))
metrics[2].metric("ID-связи с ЕГРОКН", str(len(matching["identifier_links"])) if matching else "не подключено")
metrics[3].metric("Кандидаты на проверку", str(len(matching["review_candidates"])) if matching else "не подключено")
st.caption("POI — записи источника, не уникальные заведения. ID-связь не доказывает физическую идентичность объектов.")

page = st.segmented_control(
    "Раздел", ["Обзор", "Карта", "Сопоставление E04", "Источники", "План разработки"],
    default="Обзор", key="presentation_section", selection_mode="single",
)

if page == "Обзор":
    left, right = st.columns([3, 2])
    with left, st.container(border=True):
        st.subheader("Что уже работает")
        st.markdown(
            "- Автоматический сбор границы, POI и исходных дорог по названию территории.\n"
            "- Неизменяемые снимки, происхождение данных, хэши и офлайн-повторы.\n"
            "- Частичный импорт ЕГРОКН и каталога ООПТ.\n"
            "- Проверяемые ID-связи, кандидаты и причины исключения слабых пар."
        )
        st.caption("Отдельные возможности описывают текущий код проекта. Состав именно этого запуска смотрите в разделе «Источники».")
    with right, st.container(border=True):
        st.subheader("Чего пока нет")
        st.markdown(
            "- Проверенного режимного графа и времени поездок.\n"
            "- Подтверждённых спроса D и мощности K для численного дефицита.\n"
            "- Полных правовых геометрий и проверки допустимости действий.\n"
            "- Пакетов мероприятий и прогноза прироста туристов."
        )
    st.subheader("Даты источников, а не дата презентации")
    selected_sources = [s for s in view["sources"] if s["source_id"] in (
        "geofabrik_extract", "osm_nominatim", "mkrf_egrkn", "lenobl_oopt"
    )]
    st.dataframe(
        pd.DataFrame([{"Источник": s["source_id"], "Получен (UTC)": s["retrieved_at"],
                       "Полнота снимка": s["completeness"]} for s in selected_sources]),
        hide_index=True, alt="Даты получения исходных снимков выбранного запуска",
    )
    st.caption("Полнота файла не означает полноту услуг на местности. Дата получения не устанавливает период действия правового режима.")
    st.subheader("Материалы для презентации")
    export = st.columns(2)
    export[0].download_button(
        "Скачать краткий отчёт", report_markdown(view),
        file_name=f"tourism-stage-{selected_run}.md", mime="text/markdown", width="stretch",
    )
    export[1].download_button(
        "Скачать паспорт результата (JSON)", json.dumps(summary, ensure_ascii=False, indent=2),
        file_name=f"tourism-stage-{selected_run}.json", mime="application/json", width="stretch",
    )

elif page == "Карта":
    st.subheader("Наблюдаемая пространственная база")
    st.caption(
        "Карта без внешней подложки и API-ключей. Граница запроса и область первичного сбора "
        "показаны раздельно. Дороги — исходные линии, не проверенные маршруты. "
        "Центры way/relation приблизительны, не являются входами в объекты."
    )
    st.caption("Тёмно-синий контур — граница запроса; серый контур — область первичного сбора; тонкие линии — дороги.")
    all_categories = sorted({category.strip() for row in rows for category in row["categories"].split(",") if category.strip()})
    filter_col, search_col = st.columns(2)
    categories = filter_col.multiselect("Категории наблюдений", all_categories, default=all_categories)
    search_text = search_col.text_input("Поиск POI по названию или OSM ID", key="poi_query").strip().casefold()
    filtered = [row for row in rows if any(cat.strip() in categories for cat in row["categories"].split(","))
                and (not search_text or search_text in f"{row['id']} {row['name']}".casefold())]
    show_roads = st.checkbox("Показать исходные дорожные линии", value=True)
    objects = {row["id"]: row for row in filtered}
    focus_id = st.selectbox(
        "Выделить наблюдение", [None, *objects],
        format_func=lambda identifier: "Не выделять" if identifier is None else f"{objects[identifier]['name']} · {identifier}",
        key="poi_focus",
    )
    st.vega_lite_chart(
        spec=map_spec(collection, view["network"], filtered, show_roads=show_roads, selected_id=focus_id),
        width="stretch", theme=None,
        alt="Граница территории, область сбора, дорожные линии и наблюдения POI",
    )
    st.caption(f"На карте: {len(filtered)} из {len(rows)} POI с пригодной точечной геометрией; всего записей: {len(view['poi']['features'])}. © OpenStreetMap contributors · ODbL.")
    if focus_id:
        focus = objects[focus_id]
        with st.container(border=True):
            st.text(f"{focus['name']} · {focus['id']}")
            st.text(f"Точность: {focus['precision']} · доступ: {focus['access']} · мощность: {focus['capacity']}")
            st.json(focus["tags"], expanded=False)
    st.dataframe(
        pd.DataFrame([{k: v for k, v in row.items() if k != "tags"} for row in filtered]),
        column_config={"id": "OSM ID", "name": "Название", "categories": "Категории",
                       "longitude": "Долгота", "latitude": "Широта", "precision": "Точность источника",
                       "access": "Публичный доступ", "capacity": "Мощность услуги"},
        hide_index=True, alt="Отфильтрованные исходные наблюдения POI",
    )

elif page == "Сопоставление E04":
    st.subheader("Связи и кандидаты, а не автоматическое слияние")
    if matching is None:
        st.warning("Для этого запуска обогащение ЕГРОКН не подключено или сопоставление недоступно. Это не ноль объектов наследия.")
    else:
        st.info(
            "Сопоставления рекомендательные. ID-связи, кандидаты и исключённые пары разделены. "
            "Независимая точность всей карты пока не установлена; автоматическое объединение не выполняется."
        )
        st.caption(f"Версия: {matching.get('algorithm_version', 'неизвестно')} · временное согласование источников: {matching.get('temporal_alignment', 'неизвестно')}")
        channel = st.selectbox("Канал сопоставления", ["Все", "ID-связь", "Кандидат", "Исключён из очереди"])
        query = st.text_input("Поиск пары по названию или идентификатору").strip().casefold()
        shown = [pair for pair in pairs if (channel == "Все" or pair["channel"] == channel)
                 and (not query or query in " ".join(str(v) for v in pair.values()).casefold())]
        st.dataframe(
            pd.DataFrame(shown), hide_index=True,
            column_config={"channel": "Канал", "poi_id": "OSM ID", "source_record_id": "ID записи реестра",
                           "osm_name": "Название OSM", "official_name": "Название в реестре",
                           "status": "Статус алгоритма", "distance_m": "Расстояние, м",
                           "reason": "Причина исключения", "warnings": "Оговорки"},
            alt="Связи ЕГРОКН с OSM и причины исключения кандидатов",
        )
        st.caption(f"Показано {len(shown)} из {len(pairs)} пар. Пустое расстояние означает неизвестность, не ноль метров.")
        with st.expander("Контекст адресов и ограничения алгоритма"):
            st.json(matching.get("address_locality_context", {}))
            st.json(matching.get("limitations", []))

elif page == "Источники":
    st.subheader("Происхождение и качество")
    statistics = collection.get("statistics_discovery", {})
    st.text(f"Официальная статистика: {statistics.get('status', 'неизвестно')} · причина: {statistics.get('reason') or 'не указана'}")
    st.caption("Неудача необязательного источника не заменена нулями. Каталог ООПТ не является слоем юридических границ или разрешений.")
    sources = pd.DataFrame([{
        "Источник": s["source_id"], "URL": s["url"], "Получен (UTC)": s["retrieved_at"],
        "Полнота": s["completeness"], "Версия парсера": s["parser_version"],
        "SHA-256": s["content_hash"], "Snapshot ID": s["snapshot_id"],
    } for s in view["sources"]])
    st.dataframe(sources, hide_index=True, alt="Источники, даты, версии парсеров и хэши снимков")
    with st.expander("Технический паспорт и ограничения"):
        st.json(view["run"])
        st.json(collection.get("statistics_discovery", {}))
        if view["enrichment"]:
            st.json(view["enrichment"].get("sources", {}))
    st.warning("Расчётная готовность модели не следует из успешной проверки файлов. Правовые режимы, D/K, расписания и осуществимость поездок остаются непроверенными.")

elif page == "План разработки":
    st.subheader("Проверяемый путь к поддержке решений")
    st.dataframe(pd.DataFrame([
        {"Этап": "E03 · Первичный сбор", "Состояние": "Принят", "Граница результата": "Наблюдения POI/дорог, не готовая сеть"},
        {"Этап": "E04 · Обогащение", "Состояние": "Частично", "Граница результата": "ЕГРОКН/каталог ООПТ; нужна независимая контрольная выборка"},
        {"Этап": "E05–E07 · Обновления, ограничения, сеть", "Состояние": "Предстоит", "Граница результата": "Версии, правовые фильтры, режимы, входы, недостижимость"},
        {"Этап": "E08–E09 · Услуги и поездки", "Состояние": "Предстоит", "Граница результата": "Совместная доступность, временные окна; D/K только при пригодных входах"},
        {"Этап": "E10–E12 · Мероприятия", "Состояние": "Предстоит", "Граница результата": "Создание/улучшение/обслуживание, затраты и зависимости"},
        {"Этап": "E13–E14 · Полный UI и апробация", "Состояние": "Предстоит", "Граница результата": "Этот просмотрщик не заменяет полный пользовательский анализ"},
    ]), hide_index=True, alt="Текущая готовность и следующие этапы разработки")
    st.markdown(
        "**Ближайшая содержательная проверка:** отдельная контрольная выборка E04, не использованная "
        "при настройке сопоставлений. Затем — проверка пригодности сети и доступных признаков, "
        "осуществимость поездок; генерация мер следует после проверки входов и ограничений."
    )
    st.caption("Научная новизна и превосходство метода остаются гипотезой. Процент прироста туристов не рассчитан.")

st.divider()
st.caption("Просмотр текущей разработки · архивные наблюдения · без baseline AHP/SA · без автоматических рекомендаций")
