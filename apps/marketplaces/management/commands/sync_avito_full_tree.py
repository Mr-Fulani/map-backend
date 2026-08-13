"""Management command: выгрузка ПОЛНОГО дерева категорий Avito в JSON.

В отличие от sync_avito_categories (плоский список листьев + обязательные поля),
эта команда строит вложенное дерево категорий и добавляет САМЫЙ ГЛУБОКИЙ уровень —
виды запчастей (значения полей SparePartType / EngineSparePartType /
BodySparePartType / TransmissionSparePartType) как подкатегории листа.

Результат — apps/marketplaces/data/avito_tree_<domain>.json («вшито в код»),
на его основе import_avito_tree строит каталог тенанта.

Примеры:
    python manage.py sync_avito_full_tree --root "Запчасти и аксессуары" --domain auto_parts
    python manage.py sync_avito_full_tree --root "Одежда, обувь, аксессуары" --domain apparel
"""
import json
import time
from pathlib import Path
from typing import Any

import requests
from django.core.management.base import BaseCommand, CommandError

from apps.core.url_security import ResponseTooLarge, UnsafePublicURL
from apps.marketplaces.adapters.avito.adapter import AvitoAdapter
from apps.marketplaces.avito_tree_sync import AvitoTreeSyncError, _request_avito_values
from apps.marketplaces.models import MarketplaceAccount

DATA_DIR = Path(__file__).resolve().parents[2] / 'data'


def _is_deep_tag(tag: str) -> bool:
    """Поле-вид: любой тег вида *SparePartType (Engine/Body/Transmission/… SparePartType)."""
    return bool(tag) and tag.endswith('SparePartType')


def _slugify(name: str) -> str:
    """Фолбэк-slug из имени (если у узла Avito нет slug)."""
    import re
    return re.sub(r'[^0-9a-zA-Zа-яА-ЯёЁ]+', '_', (name or '').lower()).strip('_') or 'domain'


def _children(node: dict) -> list:
    """Нормализует поле nested к списку дочерних узлов."""
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
    """Выгружает полное дерево категорий Avito (с видами запчастей) в JSON."""

    help = 'Синхронизирует полное дерево категорий Avito (с видами) в data/avito_tree_<domain>.json'

    def add_arguments(self, parser):
        parser.add_argument('--root', type=str, default=None, help='Имя корневой категории Avito')
        parser.add_argument('--domain', type=str, default=None, help='Slug домена каталога (имя файла)')
        parser.add_argument('--all', action='store_true', help='Все домены верхнего уровня Avito')
        parser.add_argument('--account', type=int, default=None, help='ID аккаунта Avito')

    def handle(self, *args, **options):
        account = self._resolve_account(options['account'])
        adapter = AvitoAdapter(account)
        tree = adapter.get_category_tree()

        if options['all']:
            for node in tree:
                self._sync_one(adapter, tree, node.get('name'),
                               node.get('slug') or _slugify(node.get('name')))
            return
        if not options['root'] or not options['domain']:
            raise CommandError('Укажите --root и --domain, либо --all')
        self._sync_one(adapter, tree, options['root'], options['domain'])

    def _sync_one(self, adapter, tree, root_name: str, domain_slug: str):
        """Собирает дерево одного корня и пишет avito_tree_<domain>.json."""
        root = _find(tree, root_name)
        if not root:
            self.stderr.write(self.style.WARNING(f'Корень «{root_name}» не найден — пропуск'))
            return
        self._leaf_count = 0
        self._deep_count = 0
        if _children(root):
            built = self._build(adapter, root)
        else:
            # Усечённый корень (Запчасти и аксессуары) — берём пути из avito_field_specs.json.
            built = self._build_from_specs(adapter, root_name)

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        out_path = DATA_DIR / f'avito_tree_{domain_slug}.json'
        out_path.write_text(
            json.dumps({'root': root_name, 'domain': domain_slug, 'tree': built['children']},
                       ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        self.stdout.write(self.style.SUCCESS(
            f'{domain_slug}: листьев {self._leaf_count}, видов {self._deep_count} → {out_path.name}'
        ))

    def _resolve_account(self, account_id):
        qs = MarketplaceAccount.objects.filter(marketplace=MarketplaceAccount.MARKETPLACE_AVITO)
        account = qs.filter(pk=account_id).first() if account_id else qs.filter(is_active=True).first()
        if not account:
            raise CommandError('Не найден аккаунт Avito для запроса к API')
        return account

    def _build(self, adapter, node: dict) -> dict:
        """Рекурсивно строит {name, slug, children}; для листьев добавляет виды запчастей."""
        children = _children(node)
        out: dict[str, Any] = {
            'name': node.get('name'),
            'slug': node.get('slug'),
            'children': [],
        }
        if children:
            for child in children:
                out['children'].append(self._build(adapter, child))
            return out
        # Лист — тянем виды запчастей как подкатегории.
        self._leaf_count += 1
        for value in self._leaf_part_types(
            adapter,
            str(node.get('slug') or ''),
            str(node.get('name') or ''),
        ):
            out['children'].append({'name': value, 'slug': None, 'children': []})
            self._deep_count += 1
        return out

    def _build_from_specs(self, adapter, root_name: str) -> dict:
        """Строит дерево из путей avito_field_specs.json (для усечённого Avito-корня)."""
        specs = json.loads((DATA_DIR / 'avito_field_specs.json').read_text(encoding='utf-8'))
        leaves = specs.get('leaves', [])
        root: dict[str, Any] = {
            'name': root_name,
            'slug': None,
            'children': [],
        }
        index: dict[tuple[str, ...], dict[str, Any]] = {}
        for leaf in leaves:
            chain = leaf.get('path', [])[1:]  # без корня
            parent = root
            acc: list[str] = []
            for depth, name in enumerate(chain):
                acc.append(name)
                key = tuple(acc)
                node = index.get(key)
                if node is None:
                    node = {'name': name, 'slug': leaf['slug'] if depth == len(chain) - 1 else None,
                            'children': []}
                    index[key] = node
                    parent['children'].append(node)
                parent = node
            # parent — лист: добавляем виды запчастей
            self._leaf_count += 1
            for value in self._leaf_part_types(adapter, leaf['slug'], leaf['name']):
                parent['children'].append({'name': value, 'slug': None, 'children': []})
                self._deep_count += 1
        return root

    def _leaf_part_types(self, adapter, slug: str, leaf_name: str) -> list[str]:
        """Возвращает виды запчастей листа (значения DEEP-полей), с ретраями на 429."""
        for attempt in range(6):
            try:
                data = adapter.get_node_fields(slug)
            except Exception as exc:  # noqa: BLE001 — 429/сеть: ретраим, остальное пропускаем
                status = getattr(getattr(exc, 'response', None), 'status_code', 0)
                if status == 429:
                    time.sleep(3 * (attempt + 1))
                    continue
                return []
            values: list[str] = []
            for field in data.get('fields', []):
                if not _is_deep_tag(field.get('tag')):
                    continue
                for content in (field.get('content') or []):
                    inline = [
                        value
                        for item in (content.get('values') or [])
                        if isinstance(item, dict)
                        and isinstance((value := item.get('value')), str)
                        and value
                    ]
                    # Часть полей (Кузов/Топливная/Электро) отдаёт виды не инлайн,
                    # а ссылкой values_link_json — догружаем её.
                    if not inline and content.get('values_link_json'):
                        inline = self._fetch_link_values(adapter, content['values_link_json'])
                    values.extend(v for v in inline if v != leaf_name)
            time.sleep(0.1)
            return list(dict.fromkeys(values))
        return []

    def _fetch_link_values(self, adapter, url: str) -> list[str]:
        """Догружает значения поля по ссылке values_link_json (с авторизацией)."""
        token = adapter._auth.get_token(adapter.account)
        for attempt in range(4):
            try:
                resp = _request_avito_values(url, token)
            except (
                AvitoTreeSyncError,
                requests.RequestException,
                ResponseTooLarge,
                UnsafePublicURL,
            ):
                return []
            if resp.status_code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            if not resp.ok:
                return []
            try:
                payload = resp.json()
            except ValueError:
                return []
            if not isinstance(payload, dict):
                return []
            values: list[str] = []
            for item in payload.get('values') or []:
                if not isinstance(item, dict):
                    continue
                value = item.get('value')
                if isinstance(value, str) and value:
                    values.append(value)
            return values
        return []
