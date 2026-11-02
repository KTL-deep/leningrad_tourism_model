"""Pure, offline presentation transforms for E03/E04 observations."""

import html
import math
import re


CATEGORY_LABELS = {
    "tourism": "Туризм",
    "accommodation": "Размещение",
    "food": "Питание",
    "transport": "Транспорт",
    "leisure": "Досуг",
    "healthcare": "Медицинские объекты",
    "historic": "Наследие",
    "sanitary": "Санитарные объекты",
    "shop": "Магазины",
}


def poi_rows(poi_fc: dict) -> list[dict]:
    """Display positioned OSM observations; category tags are not hub functions."""
    rows = []
    for feature in poi_fc["features"]:
        geometry = feature.get("geometry") or {}
        coords = geometry.get("coordinates")
        if (geometry.get("type") != "Point" or not isinstance(coords, (list, tuple))
                or len(coords) != 2 or any(type(n) not in (int, float) or not math.isfinite(n)
                                            for n in coords)
                or not -180 <= coords[0] <= 180 or not -90 <= coords[1] <= 90):
            continue
        props = feature.get("properties") or {}
        tags = props.get("tags") or {}
        categories = props.get("categories") or ["Без категории"]
        rows.append({"id": feature.get("id"), "name": tags.get("name") or feature.get("id"),
                     "categories": ", ".join(CATEGORY_LABELS.get(c, c) for c in categories),
                     "longitude": coords[0], "latitude": coords[1],
                     "precision": props.get("location_precision") or "неизвестно",
                      "access": ("неизвестно" if props.get("public_access") in (None, "", "unknown")
                                 else props["public_access"]),
                     "capacity": props.get("capacity") if props.get("capacity") is not None
                     else "неизвестно", "tags": dict(tags)})
    return rows


def matching_rows(report_or_none: dict | None) -> list[dict]:
    if report_or_none is None:
        return []
    rows = []
    for field, channel in (("identifier_links", "ID-связь"),
                           ("review_candidates", "Кандидат"),
                           ("excluded_review_candidates", "Исключён из очереди")):
        for record in report_or_none.get(field, []):
            evidence = record.get("evidence") or {}
            rows.append({"channel": channel, "poi_id": record.get("poi_id"),
                         "source_record_id": record.get("source_record_id"),
                         "osm_name": evidence.get("osm_name"),
                         "official_name": evidence.get("official_name"),
                         "status": record.get("status"), "distance_m": evidence.get("distance_m"),
                         "reason": record.get("reason", record.get("exclusion_reason")),
                         "warnings": "; ".join(evidence.get("warnings") or [])})
    return rows


def map_spec(collection: dict, network_fc: dict, rows: list[dict],
             show_roads: bool = True, selected_id: str | None = None) -> dict:
    """Inline Vega-Lite geo layers: source lines, not routes or access times."""
    area = collection["study_area"]

    def shape_layer(geometry, stroke, fill=None, width=1):
        return {"data": {"values": [{"type": "Feature", "geometry": geometry,
                                     "properties": {}}]},
                "mark": {"type": "geoshape", "fill": fill or "transparent",
                         "stroke": stroke, "strokeWidth": width}}

    layers = []
    if area.get("analysis_geometry") is not None:
        layers.append(shape_layer(area["analysis_geometry"], "#8b9aab"))
    layers.append(shape_layer(area["request_geometry"], "#244c75", "#dbeaf033", 2))
    if show_roads:
        lines = [feature for feature in network_fc["features"]
                 if (feature.get("geometry") or {}).get("type") == "LineString"]
        layers.append({"data": {"values": lines},
                       "mark": {"type": "geoshape", "fill": "transparent",
                                "stroke": "#8a8a8a", "strokeWidth": 0.8}})
    point_encoding = {"longitude": {"field": "longitude", "type": "quantitative"},
                      "latitude": {"field": "latitude", "type": "quantitative"}}
    layers.append({"data": {"values": rows}, "mark": {"type": "circle", "size": 45},
                   "encoding": {**point_encoding,
                                 "color": {"field": "categories", "type": "nominal", "title": "Категории POI"},
                                 "tooltip": [{"field": key, "type": "nominal", "title": title}
                                             for key, title in (("name", "Название"), ("id", "OSM ID"),
                                                                ("precision", "Точность"), ("access", "Доступ"))]}})
    if selected_id is not None:
        layers.append({"data": {"values": [row for row in rows if row["id"] == selected_id]},
                       "mark": {"type": "circle", "size": 160, "fill": "transparent",
                                "stroke": "#ca2719", "strokeWidth": 3},
                       "encoding": point_encoding})
    return {"$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "projection": {"type": "mercator"}, "height": 560, "width": "container",
            "layer": layers}


def summary_report(view: dict) -> dict:
    run, collection = view["run"], view["collection"]
    return {"run_id": run["run_id"], "area": run["area_id"],
            "name": collection["study_area"]["name"],
            "stage": "official_registry_enrichment" if view.get("enrichment") else "territory_collection",
            "status": run["status"], "started_at": run["started_at"],
            "poi_observation_count": len(view["poi"]["features"]),
            "road_observation_count": len(view["network"]["features"]),
            "heritage": view["matching"].get("summary") if view.get("matching") else None,
            "statistics_discovery": collection.get("statistics_discovery"),
            "sources": [{key: source.get(key) for key in
                         ("source_id", "url", "retrieved_at", "content_hash", "completeness", "snapshot_id")}
                        for source in view.get("sources", [])],
            "limitations": ["POI — наблюдения, не число заведений.",
                            "Дорожные линии не являются маршрутами.",
                             f"Первичное окружение {collection.get('surrounding_m', 'неизвестно')} м не доказывает доступность.",
                            "Нет подтверждённых D/K и правовых режимов.",
                            "Архивный снимок не является текущим обновлением.",
                            *run.get("limitations", [])]}


def _md(value) -> str:
    """Escape source text as Markdown text, never raw HTML or an active link."""
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", html.escape(str(value), quote=True))


def report_markdown(view: dict) -> str:
    report = summary_report(view)
    lines = ["# Наблюдения E03/E04 (частичный результат)",
             f"Запуск: {_md(report['run_id'])}; дата: {_md(report['started_at'])}.",
             f"Территория: {_md(report['name'])}; этап: {_md(report['stage'])}; статус: {_md(report['status'])}.",
             f"Наблюдений POI: {report['poi_observation_count']}; дорожных линий: {report['road_observation_count']}.",
             "Наблюдения POI не равны числу заведений. E03 — первичный сбор; E04 — частичное сопоставление.",
             "Рекомендации, маршруты и подтверждённые туристические эффекты не рассчитаны.",
             "Данные OpenStreetMap: © участники OSM, лицензия ODbL.",
             "## Ограничения", *[f"- {_md(item)}" for item in report["limitations"]],
             "## Происхождение источников"]
    for source in report["sources"]:
        lines.append(f"- {_md(source['source_id'])}: {_md(source['url'])}; "
                     f"SHA-256: {_md(source['content_hash'])}; "
                     f"дата получения: {_md(source['retrieved_at'])}; "
                     f"полнота: {_md(source['completeness'])}.")
    return "\n\n".join(lines[:7]) + "\n\n" + "\n".join(lines[7:]) + "\n"
