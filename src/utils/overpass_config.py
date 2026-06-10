"""
Конфигурация Overpass API сервера для osmnx.
Устанавливает зеркало вместо перегруженного overpass-api.de.
"""
from __future__ import annotations

import os

# Зеркала в порядке предпочтения. Смените на другое если текущее не работает.
OVERPASS_ENDPOINTS: list[str] = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
    "https://overpass-api.de/api/interpreter",  # оригинальный (часто перегружен)
]

# Активный сервер — меняйте здесь вручную при необходимости
ACTIVE_ENDPOINT: str = OVERPASS_ENDPOINTS[0]


def apply_overpass_config() -> None:
    """
    Применяет ACTIVE_ENDPOINT к настройкам osmnx.
    Вызывается один раз при старте main.py.
    """
    import osmnx as ox

    if hasattr(ox.settings, "overpass_url"):
        ox.settings.overpass_url = ACTIVE_ENDPOINT
    if hasattr(ox.settings, "overpass_endpoint"):
        ox.settings.overpass_endpoint = ACTIVE_ENDPOINT

    # Убираем системные прокси — они могут блокировать запросы
    os.environ.pop("HTTP_PROXY", None)
    os.environ.pop("HTTPS_PROXY", None)
    os.environ.pop("http_proxy", None)
    os.environ.pop("https_proxy", None)
    os.environ.pop("ALL_PROXY", None)
    os.environ.pop("all_proxy", None)

    # Сохраняем в env для других модулей (iduedu, topological_generator)
    os.environ["OVERPASS_URL"] = ACTIVE_ENDPOINT

    print(f"[Overpass] endpoint: {ACTIVE_ENDPOINT}")


# Обратная совместимость — старое имя функции
configure_best_overpass = apply_overpass_config
