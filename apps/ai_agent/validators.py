import json
import re

BANNED_WORDS = [
    'лучший', 'самый', 'уникальный', 'гарантия 100%', 'срочно',
    'недорого', 'дёшево', 'дешево', 'акция', 'распродажа',
]

_CONTACTS_RE = re.compile(
    r'(\+?[\d\s\-\(\)]{7,})'          # телефоны
    r'|([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})'  # email
    r'|(https?://\S+)'                 # ссылки http
    r'|(www\.\S+)',                    # ссылки www
    re.IGNORECASE,
)


class ValidationError(ValueError):
    pass


class BannedWordsError(ValidationError):
    pass


def validate_title(title: str) -> str:
    title = title.strip()
    if not (20 <= len(title) <= 100):
        raise ValidationError(f'Заголовок должен быть от 20 до 100 символов, получено {len(title)}')
    return title


def validate_description(text: str) -> str:
    text = text.strip()
    if len(text) > 7500:
        text = _truncate_at_paragraph(text, 7500)
    lower = text.lower()
    found = [w for w in BANNED_WORDS if w in lower]
    if found:
        raise BannedWordsError(f'Запрещённые слова в описании: {", ".join(found)}')
    return text


def strip_contacts(text: str) -> str:
    return _CONTACTS_RE.sub('', text).strip()


def validate_json_response(raw: str) -> dict:
    raw = raw.strip()
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
    if len(text) <= max_len:
        return text
    truncated = text[:max_len]
    last_newline = truncated.rfind('\n')
    if last_newline > max_len * 0.8:
        return truncated[:last_newline]
    return truncated
