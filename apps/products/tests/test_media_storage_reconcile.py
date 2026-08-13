import io
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.products.management.commands.reconcile_media_storage import (
    _inspect_s3_policy,
    _normalize_key,
)


class FakeStorage:
    def __init__(self):
        self.tree = {
            'dev/products': (['tenant'], []),
            'dev/products/tenant': ([], ['live.jpg', 'orphan.jpg']),
        }
        self.existing = {'dev/products/tenant/live.jpg'}
        self.deleted = []
        self.opened = []

    def listdir(self, directory):
        if directory not in self.tree:
            raise FileNotFoundError(directory)
        return self.tree[directory]

    def exists(self, key):
        return key in self.existing

    def open(self, key, mode):
        self.opened.append((key, mode))
        return io.BytesIO(b'x')

    def delete(self, key):
        self.deleted.append(key)


def test_media_reconcile_is_read_only_by_default(settings):
    settings.MEDIA_KEY_PREFIX = 'dev'
    storage = FakeStorage()
    references = {
        'dev/products/tenant/live.jpg',
        'dev/catalog-categories/tenant/missing.jpg',
    }
    output = io.StringIO()

    with (
        patch(
            'apps.products.management.commands.reconcile_media_storage.default_storage',
            storage,
        ),
        patch(
            'apps.products.management.commands.reconcile_media_storage._collect_referenced_keys',
            return_value=(references, []),
        ),
    ):
        call_command(
            'reconcile_media_storage',
            max_objects=10,
            max_references=10,
            read_sample=1,
            stdout=output,
        )

    report = output.getvalue()
    assert 'missing_references: 1' in report
    assert 'orphan_objects: 1' in report
    assert 'mode: read-only' in report
    assert storage.deleted == []
    assert storage.opened == [('dev/products/tenant/live.jpg', 'rb')]


def test_media_reconcile_does_not_head_reference_outside_managed_prefixes(settings):
    settings.MEDIA_KEY_PREFIX = 'dev'
    storage = FakeStorage()
    references = {
        'dev/products/tenant/live.jpg',
        'unmanaged/private/secret.jpg',
    }
    storage.exists = lambda key: (_ for _ in ()).throw(
        AssertionError('unmanaged key must never be requested'),
    ) if key.startswith('unmanaged/') else True
    output = io.StringIO()

    with (
        patch(
            'apps.products.management.commands.reconcile_media_storage.default_storage',
            storage,
        ),
        patch(
            'apps.products.management.commands.reconcile_media_storage._collect_referenced_keys',
            return_value=(references, []),
        ),
    ):
        call_command(
            'reconcile_media_storage',
            max_objects=10,
            max_references=10,
            stdout=output,
        )

    assert 'invalid_references: 1' in output.getvalue()


@pytest.mark.django_db
def test_media_reconcile_deletes_only_with_double_confirmation(settings):
    settings.MEDIA_KEY_PREFIX = 'dev'
    storage = FakeStorage()
    references = {'dev/products/tenant/live.jpg'}

    with (
        patch(
            'apps.products.management.commands.reconcile_media_storage.default_storage',
            storage,
        ),
        patch(
            'apps.products.management.commands.reconcile_media_storage._collect_referenced_keys',
            return_value=(references, []),
        ),
    ):
        call_command(
            'reconcile_media_storage',
            max_objects=10,
            max_references=10,
            delete_orphans=True,
            maintenance_mode_confirmed=True,
            max_deletes=1,
        )

    assert storage.deleted == ['dev/products/tenant/orphan.jpg']


def test_media_reconcile_rejects_single_delete_confirmation():
    with pytest.raises(CommandError, match='requires both'):
        call_command('reconcile_media_storage', delete_orphans=True)


def test_media_key_normalization_rejects_traversal():
    assert _normalize_key('products/tenant/file.jpg') == 'products/tenant/file.jpg'
    assert _normalize_key('/products/tenant/file.jpg') == ''
    assert _normalize_key('https://bucket.example/file.jpg') == ''
    assert _normalize_key(r'products\tenant\file.jpg') == ''
    assert _normalize_key('../private/key') == ''
    assert _normalize_key('products/../private/key') == ''


def test_media_bucket_contract_requires_versioning_and_noncurrent_lifecycle():
    class Client:
        def get_bucket_versioning(self, **kwargs):
            return {'Status': 'Enabled'}

        def get_bucket_lifecycle_configuration(self, **kwargs):
            return {
                'Rules': [{
                    'ID': 'expire-noncurrent-media',
                    'Status': 'Enabled',
                    'Filter': {'Prefix': ''},
                    'NoncurrentVersionExpiration': {'NoncurrentDays': 365},
                    'AbortIncompleteMultipartUpload': {'DaysAfterInitiation': 7},
                }],
            }

    storage = SimpleNamespace(bucket=SimpleNamespace(
        name='media-test',
        meta=SimpleNamespace(client=Client()),
    ))

    policy = _inspect_s3_policy(storage)

    assert policy['versioning'] == 'Enabled'
    assert policy['noncurrent_expiration'] is True
    assert policy['compliant'] is True


def test_media_bucket_contract_rejects_current_object_expiration():
    class Client:
        def get_bucket_versioning(self, **kwargs):
            return {'Status': 'Enabled'}

        def get_bucket_lifecycle_configuration(self, **kwargs):
            return {
                'Rules': [{
                    'ID': 'unsafe-live-media-expiration',
                    'Status': 'Enabled',
                    'Expiration': {'Days': 30},
                    'NoncurrentVersionExpiration': {'NoncurrentDays': 365},
                    'AbortIncompleteMultipartUpload': {'DaysAfterInitiation': 7},
                }],
            }

    storage = SimpleNamespace(bucket=SimpleNamespace(
        name='media-test',
        meta=SimpleNamespace(client=Client()),
    ))

    policy = _inspect_s3_policy(storage)

    assert policy['current_expiration'] is True
    assert policy['compliant'] is False


@pytest.mark.parametrize('rule', [
    {
        'ID': 'too-short-retention',
        'Status': 'Enabled',
        'Filter': {'Prefix': ''},
        'NoncurrentVersionExpiration': {'NoncurrentDays': 30},
        'AbortIncompleteMultipartUpload': {'DaysAfterInitiation': 7},
    },
    {
        'ID': 'unrelated-prefix',
        'Status': 'Enabled',
        'Filter': {'Prefix': 'unrelated'},
        'NoncurrentVersionExpiration': {'NoncurrentDays': 365},
        'AbortIncompleteMultipartUpload': {'DaysAfterInitiation': 7},
    },
    {
        'ID': 'missing-multipart-cleanup',
        'Status': 'Enabled',
        'Filter': {'Prefix': ''},
        'NoncurrentVersionExpiration': {'NoncurrentDays': 365},
    },
])
def test_media_bucket_contract_rejects_incomplete_retention_rules(rule):
    class Client:
        def get_bucket_versioning(self, **kwargs):
            return {'Status': 'Enabled'}

        def get_bucket_lifecycle_configuration(self, **kwargs):
            return {'Rules': [rule]}

    storage = SimpleNamespace(bucket=SimpleNamespace(
        name='media-test',
        meta=SimpleNamespace(client=Client()),
    ))

    assert _inspect_s3_policy(storage)['compliant'] is False
