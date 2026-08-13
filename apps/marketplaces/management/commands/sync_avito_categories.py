"""Management command: выгрузка справочника категорий и полей Avito Autoload в JSON.

Тянет дерево категорий и обязательные поля каждого листа под корневым узлом
(по умолчанию «Запчасти и аксессуары») и сохраняет компактный справочник в
apps/marketplaces/data/avito_field_specs.json. На него опираются маппинг
категорий и валидация фида перед публикацией.
"""
import json
import time
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.marketplaces.adapters.avito.adapter import AvitoAdapter
from apps.marketplaces.models import MarketplaceAccount

DATA_PATH = Path(__file__).resolve().parents[2] / 'data' / 'avito_field_specs.json'
DEFAULT_ROOT = 'Запчасти и аксессуары'


def _children(node: dict) -> list:
    """Возвращает список дочерних узлов, нормализуя возможные формы поля nested."""
    nested = node.get('nested') or []
    if isinstance(nested, dict):
        nested = [v for vals in nested.values() for v in (vals if isinstance(vals, list) else [vals])]
    return [x for x in nested if isinstance(x, dict)]


def _find(nodes: list, name: str):
    """Ищет узел по имени в глубину."""
    for node in nodes:
        if node.get('name') == name:
            return node
        found = _find(_children(node), name)
        if found:
            return found
    return None


class Command(BaseCommand):
    """Выгружает дерево и обязательные поля категорий Avito в JSON-справочник."""

    help = 'Синхронизирует справочник категорий/полей Avito Autoload в data/avito_field_specs.json'

    def add_arguments(self, parser):
        """Параметры: --account (ID аккаунта Avito), --root (имя корневой категории)."""
        parser.add_argument('--account', type=int, default=None, help='ID аккаунта Avito')
        parser.add_argument('--root', type=str, default=DEFAULT_ROOT, help='Имя корневой категории')

    def handle(self, *args, **options):
        """Тянет дерево, обходит листья корневого узла и пишет справочник в JSON."""
        account = self._resolve_account(options['account'])
        adapter = AvitoAdapter(account)

        categories = adapter.get_category_tree()
        root = _find(categories, options['root'])
        if not root:
            raise CommandError(f'Корневая категория «{options["root"]}» не найдена в дереве Avito')

        leaves: list[dict[str, Any]] = []
        self._collect_leaves(root, [], leaves)
        self.stdout.write(f'Найдено листьев: {len(leaves)}')

        result, errors = [], 0
        for leaf in leaves:
            required, fixed, http = self._leaf_fields(adapter, leaf['slug'])
            if http != 200:
                errors += 1
            result.append({**leaf, 'required': required, 'fixed': fixed})
            time.sleep(0.12)

        DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        DATA_PATH.write_text(
            json.dumps({'root': root.get('slug'), 'leaf_count': len(result), 'leaves': result},
                       ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        self.stdout.write(self.style.SUCCESS(
            f'Готово: {len(result)} листьев записано в {DATA_PATH.name}, ошибок: {errors}'
        ))

    def _resolve_account(self, account_id):
        """Возвращает аккаунт по ID или первый активный аккаунт Avito."""
        qs = MarketplaceAccount.objects.filter(marketplace=MarketplaceAccount.MARKETPLACE_AVITO)
        account = qs.filter(pk=account_id).first() if account_id else qs.filter(is_active=True).first()
        if not account:
            raise CommandError('Не найден аккаунт Avito для запроса к API')
        return account

    def _collect_leaves(self, node, path, acc):
        """Рекурсивно собирает листовые узлы (без детей) с путём до них."""
        children = _children(node)
        new_path = path + [node.get('name')]
        if not children:
            acc.append({'slug': node.get('slug'), 'name': node.get('name'), 'path': new_path})
        for child in children:
            self._collect_leaves(child, new_path, acc)

    def _leaf_fields(self, adapter, slug):
        """Возвращает (required_tags, fixed_values, http_status) для листа; ретрай на 429."""
        for _ in range(3):
            try:
                data = adapter.get_node_fields(slug)
            except Exception as exc:  # noqa: BLE001 — сетевые/HTTP-сбои логируем и продолжаем
                status = getattr(getattr(exc, 'response', None), 'status_code', 0)
                if status == 429:
                    time.sleep(2)
                    continue
                return [], {}, status or -1
            required, fixed = [], {}
            for field in data.get('fields', []):
                tag = field.get('tag')
                for content in (field.get('content') or []):
                    if not content.get('required'):
                        continue
                    if tag not in required:
                        required.append(tag)
                    values = [v.get('value') for v in (content.get('values') or [])]
                    if len(values) == 1:
                        fixed[tag] = values[0]
            return required, fixed, 200
        return [], {}, 429
