"""Настройки модуля поиска изображений.

Подключается в base.py через: from config.settings.image_search import *
"""

# Список активных источников (в порядке tier)
IMAGE_SOURCES_ENABLED = [
    'duckduckgo',   # Tier 4 — единственный активный источник MVP
    # 'autodoc',    # Tier 1 — изображения с вотермарком autodoc.ru, непригодны для объявлений
    # 'exist',      # Tier 2 — не проверен (возможно SPA)
    # 'emex',       # Tier 3 — не проверен (возможно SPA)
    # 'tecdoc',     # ← добавить когда будет API-ключ
    # 'google_cse', # ← добавить когда будет бюджет
]

IMAGE_SEARCH_SETTINGS = {
    'MAX_IMAGES_PER_PRODUCT': 3,       # Максимум изображений на товар
    'MIN_QUALITY_SCORE':      0.45,    # Минимальный score для принятия
    'AUTO_APPROVE_THRESHOLD': 0.75,    # Порог автоматического одобрения
    'SCRAPING_DELAY_SEC':     1.5,     # Задержка между запросами к источникам
    'DOWNLOAD_TIMEOUT_SEC':   10,      # Таймаут скачивания изображения
    'MAX_FILE_SIZE_MB':       5,       # Максимальный размер файла
    'MIN_RESOLUTION':         300,     # Минимальное разрешение (px)
    'CACHE_TTL_DAYS':         7,       # TTL кеша результатов поиска

    # Размеры версий изображений
    'ORIGINAL_MAX_PX':  1600,
    'PREVIEW_MAX_PX':   600,
    'THUMB_MAX_PX':     150,
    'JPEG_QUALITY_ORIGINAL': 90,
    'JPEG_QUALITY_PREVIEW':  85,
    'JPEG_QUALITY_THUMB':    80,
}
