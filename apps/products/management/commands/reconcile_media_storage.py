import posixpath
from contextlib import closing

from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from apps.media_processing.models import ProductImageVariant
from apps.products.models import ProductImage, TenantCatalogCategory
from apps.products.storage import media_storage_key


def _normalize_key(value: object) -> str:
    key = str(value or '').strip()
    if key.startswith('/') or '://' in key or '\\' in key:
        return ''
    if not key or key != posixpath.normpath(key) or key.startswith('../'):
        return ''
    return key


def _collect_referenced_keys(max_references: int) -> tuple[set[str], list[str]]:
    keys: set[str] = set()
    invalid: list[str] = []
    rows = (
        ProductImage.objects.values_list('s3_key', 's3_key_preview', 's3_key_thumb'),
        ProductImageVariant.objects.values_list('s3_key'),
        TenantCatalogCategory.objects.exclude(
            default_image_s3_key='',
        ).values_list('default_image_s3_key'),
    )
    for queryset in rows:
        for row in queryset.iterator(chunk_size=1000):
            for raw_key in row:
                if not raw_key:
                    continue
                key = _normalize_key(raw_key)
                if not key:
                    invalid.append(str(raw_key))
                    continue
                keys.add(key)
                if len(keys) > max_references:
                    raise CommandError(
                        f'Media reference limit exceeded ({max_references}); '
                        'increase --max-references explicitly.',
                    )
    return keys, invalid


def _managed_prefixes() -> tuple[str, ...]:
    # Keep legacy un-namespaced prefixes in the audit until all old DB records
    # have naturally rotated out. Only these application-owned trees are read.
    candidates = (
        media_storage_key('products'),
        media_storage_key('catalog-categories'),
        'products',
        'catalog-categories',
    )
    return tuple(dict.fromkeys(_normalize_key(prefix) for prefix in candidates if prefix))


def _walk_storage(storage, prefixes: tuple[str, ...], max_objects: int) -> set[str]:
    objects: set[str] = set()
    visited: set[str] = set()
    stack = list(reversed(prefixes))
    while stack:
        directory = stack.pop()
        if directory in visited:
            continue
        visited.add(directory)
        try:
            directories, files = storage.listdir(directory)
        except FileNotFoundError:
            continue
        except (NotImplementedError, AttributeError) as exc:
            raise CommandError(
                'Configured storage does not support bounded prefix listing.',
            ) from exc
        except Exception as exc:
            raise CommandError(f'Cannot list media prefix {directory!r}: {exc}') from exc

        for filename in files:
            key = _normalize_key(posixpath.join(directory, str(filename)))
            if not key or not any(key == prefix or key.startswith(f'{prefix}/') for prefix in prefixes):
                raise CommandError(f'Storage returned an unsafe object key: {filename!r}')
            objects.add(key)
            if len(objects) > max_objects:
                raise CommandError(
                    f'Media object limit exceeded ({max_objects}); '
                    'increase --max-objects explicitly.',
                )
        for child in reversed(directories):
            child_key = _normalize_key(posixpath.join(directory, str(child)))
            if not child_key or not any(
                child_key == prefix or child_key.startswith(f'{prefix}/')
                for prefix in prefixes
            ):
                raise CommandError(f'Storage returned an unsafe directory key: {child!r}')
            stack.append(child_key)
    return objects


def _key_is_referenced(key: str) -> bool:
    return (
        ProductImage.objects.filter(
            Q(s3_key=key) | Q(s3_key_preview=key) | Q(s3_key_thumb=key),
        ).exists()
        or ProductImageVariant.objects.filter(s3_key=key).exists()
        or TenantCatalogCategory.objects.filter(default_image_s3_key=key).exists()
    )


def _lifecycle_prefix(rule: dict) -> str | None:
    """Return an untagged prefix filter; None means unsupported/ambiguous."""
    rule_filter = rule.get('Filter')
    if rule_filter is None:
        raw_prefix = rule.get('Prefix', '')
        return _normalize_key(raw_prefix) if raw_prefix else ''
    if not isinstance(rule_filter, dict) or set(rule_filter) - {'Prefix'}:
        return None
    raw_prefix = rule_filter.get('Prefix', '')
    if not isinstance(raw_prefix, str):
        return None
    return _normalize_key(raw_prefix) if raw_prefix else ''


def _prefix_covers(rule_prefix: str, managed_prefix: str) -> bool:
    return not rule_prefix or (
        managed_prefix == rule_prefix
        or managed_prefix.startswith(f'{rule_prefix}/')
    )


def _prefix_overlaps(rule_prefix: str | None, managed_prefix: str) -> bool:
    # Unsupported tag/size filters may select managed objects; fail closed.
    return rule_prefix is None or _prefix_covers(rule_prefix, managed_prefix) or (
        rule_prefix.startswith(f'{managed_prefix}/')
    )


def _inspect_s3_policy(storage) -> dict[str, object]:
    try:
        bucket = storage.bucket
        client = bucket.meta.client
        bucket_name = bucket.name
    except (AttributeError, TypeError) as exc:
        raise CommandError('Configured media storage is not an S3 bucket backend.') from exc

    try:
        versioning = client.get_bucket_versioning(Bucket=bucket_name).get('Status', '')
        lifecycle = client.get_bucket_lifecycle_configuration(Bucket=bucket_name)
        rules = [rule for rule in lifecycle.get('Rules', []) if rule.get('Status') == 'Enabled']
    except Exception as exc:
        error_code = getattr(exc, 'response', {}).get('Error', {}).get('Code', '')
        if error_code == 'NoSuchLifecycleConfiguration':
            rules = []
        else:
            raise CommandError(f'Cannot inspect media bucket policy: {exc}') from exc

    managed_prefixes = _managed_prefixes()
    prefix_contracts: dict[str, bool] = {}
    for managed_prefix in managed_prefixes:
        prefix_contracts[managed_prefix] = any(
            (rule_prefix := _lifecycle_prefix(rule)) is not None
            and _prefix_covers(rule_prefix, managed_prefix)
            and int(
                rule.get('NoncurrentVersionExpiration', {}).get('NoncurrentDays') or 0
            ) >= 365
            and 0 < int(
                rule.get('AbortIncompleteMultipartUpload', {}).get(
                    'DaysAfterInitiation',
                ) or 0
            ) <= 7
            for rule in rules
        )
    unsafe_current_expiration = any(
        'Expiration' in rule
        and any(
            _prefix_overlaps(_lifecycle_prefix(rule), managed_prefix)
            for managed_prefix in managed_prefixes
        )
        for rule in rules
    )
    noncurrent_expiration = bool(prefix_contracts) and all(prefix_contracts.values())
    current_expiration = unsafe_current_expiration
    return {
        'versioning': versioning,
        'enabled_lifecycle_rules': len(rules),
        'noncurrent_expiration': noncurrent_expiration,
        'current_expiration': current_expiration,
        # Live objects are referenced by the database and must never expire by
        # age. Retention is allowed only for superseded/noncurrent versions.
        'compliant': (
            versioning == 'Enabled'
            and noncurrent_expiration
            and not unsafe_current_expiration
        ),
    }


class Command(BaseCommand):
    help = (
        'Read-only by default: compare DB media references with storage, '
        'optionally sample-read objects and inspect S3 versioning/lifecycle.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--max-objects', type=int, default=100000)
        parser.add_argument('--max-references', type=int, default=100000)
        parser.add_argument('--show-limit', type=int, default=50)
        parser.add_argument('--read-sample', type=int, default=0)
        parser.add_argument('--check-s3-policy', action='store_true')
        parser.add_argument('--fail-on-drift', action='store_true')
        parser.add_argument('--delete-orphans', action='store_true')
        parser.add_argument('--maintenance-mode-confirmed', action='store_true')
        parser.add_argument('--max-deletes', type=int, default=100)

    def handle(self, *args, **options):
        for name in ('max_objects', 'max_references', 'show_limit', 'max_deletes'):
            if options[name] < 1:
                raise CommandError(f'--{name.replace("_", "-")} must be positive.')
        if not 0 <= options['read_sample'] <= 1000:
            raise CommandError('--read-sample must be between 0 and 1000.')
        if options['delete_orphans'] != options['maintenance_mode_confirmed']:
            raise CommandError(
                'Deletion requires both --delete-orphans and '
                '--maintenance-mode-confirmed. Stop media uploads first.',
            )

        references, invalid_references = _collect_referenced_keys(
            options['max_references'],
        )
        prefixes = _managed_prefixes()
        stored_objects = _walk_storage(default_storage, prefixes, options['max_objects'])

        missing = []
        for key in sorted(references):
            if not any(
                key == prefix or key.startswith(f'{prefix}/')
                for prefix in prefixes
            ):
                invalid_references.append(key)
                continue
            try:
                if not default_storage.exists(key):
                    missing.append(key)
            except Exception as exc:
                raise CommandError(f'Cannot HEAD referenced media {key!r}: {exc}') from exc
        orphans = sorted(stored_objects - references)

        unreadable = []
        readable_candidates = [key for key in sorted(references) if key not in set(missing)]
        for key in readable_candidates[:options['read_sample']]:
            try:
                with closing(default_storage.open(key, 'rb')) as media_file:
                    media_file.read(1)
            except Exception:
                unreadable.append(key)

        policy = None
        if options['check_s3_policy']:
            policy = _inspect_s3_policy(default_storage)

        self.stdout.write(f'managed_prefixes: {", ".join(prefixes)}')
        self.stdout.write(f'db_references: {len(references)}')
        self.stdout.write(f'stored_objects: {len(stored_objects)}')
        self.stdout.write(f'missing_references: {len(missing)}')
        self.stdout.write(f'orphan_objects: {len(orphans)}')
        self.stdout.write(f'invalid_references: {len(invalid_references)}')
        self.stdout.write(f'unreadable_sample: {len(unreadable)}')
        if policy is not None:
            self.stdout.write(f'bucket_versioning: {policy["versioning"] or "disabled"}')
            self.stdout.write(
                f'enabled_lifecycle_rules: {policy["enabled_lifecycle_rules"]}',
            )
            self.stdout.write(
                f'noncurrent_version_expiration: {policy["noncurrent_expiration"]}',
            )
            if policy['current_expiration']:
                self.stdout.write(self.style.WARNING(
                    'warning: current-version expiration exists; ensure it cannot '
                    'expire live DB-referenced media.',
                ))

        show_limit = options['show_limit']
        for label, values in (
            ('missing', missing),
            ('orphan', orphans),
            ('invalid', invalid_references),
            ('unreadable', unreadable),
        ):
            for value in values[:show_limit]:
                self.stdout.write(f'{label}: {value}')
            if len(values) > show_limit:
                self.stdout.write(f'{label}: ... {len(values) - show_limit} more')

        if options['delete_orphans']:
            if len(orphans) > options['max_deletes']:
                raise CommandError(
                    f'Refusing to delete {len(orphans)} objects; '
                    f'--max-deletes is {options["max_deletes"]}.',
                )
            deleted = 0
            for key in orphans:
                # Re-check immediately before deletion. The maintenance-mode
                # confirmation is still mandatory to exclude an upload race.
                if _key_is_referenced(key):
                    continue
                try:
                    default_storage.delete(key)
                except Exception as exc:
                    raise CommandError(f'Cannot delete orphan {key!r}: {exc}') from exc
                deleted += 1
            self.stdout.write(self.style.SUCCESS(f'deleted_orphans: {deleted}'))
        else:
            self.stdout.write('mode: read-only')

        drift = bool(missing or orphans or invalid_references or unreadable)
        if policy is not None:
            drift = drift or not policy['compliant']
        if options['fail_on_drift'] and drift:
            raise CommandError('Media storage drift detected.')
