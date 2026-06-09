import json
import re

BANNED_WORDS = [
    'лучший', 'самый', 'уникальный', 'гарантия 100%', 'срочно',
    'недорого', 'дёшево', 'дешево', 'акция', 'распродажа',
]

VAGUE_FITMENT_PHRASES = [
    'для различных моделей',
    'для разных моделей',
    'для многих моделей',
    'для некоторых моделей',
    'для большинства моделей',
    'для широкого спектра',
    'для широкого круга',
    'подходит для различных',
    'подходит для разных',
    'подходит для многих',
    'подходит для некоторых',
    'подойдут для различных',
    'подойдут для разных',
    'подойдут для некоторых',
    'некоторых моделей автомобилей',
    'совместим с различными',
    'совместима с различными',
    'совместимы с различными',
]

# Регулярка для удаления контактных данных из текста объявления
_CONTACTS_RE = re.compile(
    r'(\+?[\d\s\-\(\)]{7,})'          # телефоны
    r'|([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})'  # email
    r'|(https?://\S+)'                 # ссылки http
    r'|(www\.\S+)',                    # ссылки www
    re.IGNORECASE,
)


class ValidationError(ValueError):
    """Ошибка валидации ответа AI-агента."""


class BannedWordsError(ValidationError):
    """В тексте обнаружены запрещённые слова."""


class VagueFitmentError(ValidationError):
    """AI написал неконкретную применяемость вместо фактов."""


def validate_title(title: str) -> str:
    """Проверяет длину заголовка (50–200 символов)."""
    title = title.strip()
    if not (50 <= len(title) <= 200):
        raise ValidationError(f'Заголовок должен быть от 50 до 200 символов, получено {len(title)}')
    return title


_MARKDOWN_BOLD_RE = re.compile(r'\*\*(.+?)\*\*')


def strip_markdown(text: str) -> str:
    """Убирает markdown-форматирование: **bold**, *italic*, # заголовки."""
    text = _MARKDOWN_BOLD_RE.sub(r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    return text


def validate_description(text: str) -> str:
    """Обрезает описание до 7500 символов и проверяет запрещённые слова."""
    text = strip_markdown(text.strip())
    if len(text) > 7500:
        text = _truncate_at_paragraph(text, 7500)
    lower = text.lower()
    found = [w for w in BANNED_WORDS if w in lower]
    if found:
        raise BannedWordsError(f'Запрещённые слова в описании: {", ".join(found)}')
    vague_fitment = [phrase for phrase in VAGUE_FITMENT_PHRASES if phrase in lower]
    if vague_fitment:
        raise VagueFitmentError(
            f'Неконкретная применяемость в описании: {", ".join(vague_fitment)}'
        )
    return text


def strip_contacts(text: str) -> str:
    """Удаляет телефоны, email и ссылки из текста."""
    def replace(match):
        phone = match.group(1)
        if phone and len(re.sub(r'\D', '', phone)) < 7:
            return match.group(0)
        return ''

    return _CONTACTS_RE.sub(replace, text).strip()


def validate_json_response(raw: str) -> dict:
    """Парсит JSON-ответ агента, проверяет структуру и применяет все валидации."""
    raw = raw.strip()
    # Убираем markdown-блоки если модель их добавила
    if raw.startswith('```'):
        raw = re.sub(r'^```[a-z]*\n?', '', raw)
        raw = re.sub(r'\n?```$', '', raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValidationError(f'Не удалось разобрать JSON: {e}')

    for field in ('title', 'description', 'confidence'):
        if field not in data:
            raise ValidationError(f'Отсутствует поле: {field}')

    data['title'] = validate_title(str(data['title']))
    data['description'] = strip_contacts(str(data['description']))
    data['description'] = validate_description(data['description'])

    try:
        data['confidence'] = float(data['confidence'])
    except (TypeError, ValueError):
        data['confidence'] = 0.5

    return data


def _truncate_at_paragraph(text: str, max_len: int) -> str:
    """Обрезает текст по последнему переносу строки до max_len символов."""
    if len(text) <= max_len:
        return text
    truncated = text[:max_len]
    last_newline = truncated.rfind('\n')
    if last_newline > max_len * 0.8:
        return truncated[:last_newline]
    return truncated
