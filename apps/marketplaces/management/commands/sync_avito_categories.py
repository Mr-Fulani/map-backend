"""Management command: выгрузка справочника категорий и полей Avito Autoload в JSON.

Тянет дерево категорий и обязательные поля каждого листа под корневым узлом
(по умолчанию «Запчасти и аксессуары») и сохраняет компактный справочник в
apps/marketplaces/data/avito_field_specs.json. На него опираются маппинг
категорий и валидация фида перед публикацией.
"""
import gzip
import json
import time
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.marketplaces.adapters.avito.adapter import AvitoAdapter
from apps.marketplaces.models import MarketplaceAccount

DATA_PATH = Path(__file__).resolve().parents[2] / 'data' / 'avito_field_specs.json'
RULES_PATH = DATA_PATH.with_name('avito_field_rules.json.gz')
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


def _dependency_rules(content: dict) -> list[dict]:
    """Keep the schema predicates needed to decide whether a field applies.

    Avito marks many fields as ``required_by_dependency`` instead of setting
    ``required`` directly (for example OEM/Brand when Condition=New).  Dropping
    these predicates makes local preflight disagree with the provider.
    """
    result = []
    for dependency in content.get('dependencies') or []:
        action = dependency.get('action')
        expression = dependency.get('expression') or dependency
        if action not in {'required', 'visible', 'hidden'}:
            continue
        pairs = []
        for pair in expression.get('pairs') or []:
            clause = pair.get('clause')
            tag = pair.get('tag') or pair.get('source_field_tag')
            if not tag or clause not in {'empty', 'filled', 'value'}:
                continue
            values = []
            for item in pair.get('values') or []:
                value = item.get('value') if isinstance(item, dict) else item
                if value is not None:
                    values.append(str(value))
            pairs.append({'tag': tag, 'clause': clause, 'values': values})
        if pairs:
            result.append({
                'action': action,
                'clause': expression.get('clause') if expression.get('clause') in {'and', 'or'} else 'and',
                'pairs': pairs,
            })
    return result


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

        existing, existing_rules = {}, {}
        if DATA_PATH.exists():
            try:
                existing = {
                    leaf['slug']: leaf
                    for leaf in json.loads(DATA_PATH.read_text(encoding='utf-8')).get('leaves', [])
                }
            except (KeyError, TypeError, ValueError):
                existing = {}
        if existing and len(leaves) < len(existing):
            self.stdout.write(self.style.WARNING(
                'API вернул неполное дерево; обновляем сохранённый inventory листьев.',
            ))
            leaves = [
                {key: item[key] for key in ('slug', 'name', 'path')}
                for item in existing.values()
            ]
        if RULES_PATH.exists():
            try:
                existing_rules = json.loads(gzip.decompress(RULES_PATH.read_bytes()))
            except (OSError, TypeError, ValueError):
                existing_rules = {}

        result, collected_rules, errors = [], {}, 0
        for leaf in leaves:
            required, fixed, field_rules, http = self._leaf_fields(adapter, leaf['slug'])
            if http != 200:
                errors += 1
                previous = existing.get(leaf['slug']) or {}
                required = previous.get('required', required)
                fixed = previous.get('fixed', fixed)
                field_rules = existing_rules.get(leaf['slug'], field_rules)
            result.append({
                **leaf,
                'required': required,
                'fixed': fixed,
                'http': http,
            })
            if field_rules:
                collected_rules[leaf['slug']] = field_rules
            time.sleep(0.12)

        DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        DATA_PATH.write_text(
            json.dumps({'root': root.get('slug'), 'leaf_count': len(result), 'leaves': result},
                       ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        RULES_PATH.write_bytes(gzip.compress(
            json.dumps(collected_rules, ensure_ascii=False, separators=(',', ':')).encode(),
            mtime=0,
        ))
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
        """Return static fields, conditional rules and HTTP status for a leaf."""
        for _ in range(3):
            try:
                data = adapter.get_node_fields(slug)
            except Exception as exc:  # noqa: BLE001 — сетевые/HTTP-сбои логируем и продолжаем
                status = getattr(getattr(exc, 'response', None), 'status_code', 0)
                if status == 429:
                    time.sleep(2)
                    continue
                return [], {}, {}, status or -1
            required, fixed, field_rules = [], {}, {}
            for field in data.get('fields', []):
                tag = field.get('tag')
                variants = []
                for content in (field.get('content') or []):
                    is_required = bool(content.get('required'))
                    required_by_dependency = bool(content.get('required_by_dependency'))
                    rules = _dependency_rules(content)
                    if is_required and not required_by_dependency and tag not in required:
                        required.append(tag)
                    if is_required and not required_by_dependency:
                        values = [v.get('value') for v in (content.get('values') or [])]
                        if len(values) == 1:
                            fixed[tag] = values[0]
                    variants.append({
                        'required': is_required and not required_by_dependency,
                        'dependencies': rules,
                    })
                if any(variant['dependencies'] for variant in variants):
                    field_rules[tag] = variants
            return required, fixed, field_rules, 200
        return [], {}, {}, 429
