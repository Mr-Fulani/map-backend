import ipaddress
from urllib.parse import urlsplit, urlunsplit

from django.db import migrations, models
from django.db.models import Count, Q
from django.utils import timezone


def _canonical_https_url(value):
    raw_url = str(value or '').strip()
    if (
        not raw_url
        or len(raw_url) > 500
        or '\\' in raw_url
        or any(ord(char) < 32 or ord(char) == 127 for char in raw_url)
    ):
        return None
    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
        hostname = (parsed.hostname or '').rstrip('.').lower()
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme.lower() != 'https'
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    try:
        canonical_hostname = ipaddress.ip_address(hostname).compressed
    except ValueError:
        try:
            canonical_hostname = hostname.encode('idna').decode('ascii')
        except UnicodeError:
            return None
    host = (
        f'[{canonical_hostname}]'
        if ':' in canonical_hostname
        else canonical_hostname
    )
    authority = host if port in (None, 443) else f'{host}:{port}'
    canonical = urlunsplit((
        'https',
        authority,
        parsed.path or '/',
        parsed.query,
        '',
    ))
    return canonical if len(canonical) <= 500 else None


def retire_unsafe_and_duplicate_endpoints(apps, schema_editor):
    """Preserve delivery history while making live endpoints constraint-safe."""
    WebhookEndpoint = apps.get_model('tenants', 'WebhookEndpoint')
    retired_at = timezone.now()

    live_endpoints = WebhookEndpoint.objects.filter(deleted_at__isnull=True)
    for endpoint in live_endpoints.iterator():
        canonical_url = _canonical_https_url(endpoint.url)
        if canonical_url is not None:
            if endpoint.url != canonical_url:
                endpoint.url = canonical_url
                endpoint.save(update_fields=['url'])
            continue
        WebhookEndpoint.objects.filter(pk=endpoint.pk).update(
            is_active=False,
            deleted_at=retired_at,
            updated_at=retired_at,
        )

    duplicate_groups = (
        WebhookEndpoint.objects.filter(deleted_at__isnull=True)
        .values('tenant_id', 'url')
        .annotate(endpoint_count=Count('pk'))
        .filter(endpoint_count__gt=1)
    )
    for group in duplicate_groups.iterator():
        duplicates = WebhookEndpoint.objects.filter(
            tenant_id=group['tenant_id'],
            url=group['url'],
            deleted_at__isnull=True,
        ).order_by('-is_active', 'created_at', 'pk')
        keeper = duplicates.first()
        duplicates.exclude(pk=keeper.pk).update(
            is_active=False,
            deleted_at=retired_at,
            updated_at=retired_at,
        )


class Migration(migrations.Migration):

    # PostgreSQL cannot ALTER this table while FK trigger events from the data
    # cleanup are still pending in the same transaction. The cleanup is
    # idempotent, so commit it before adding the two constraints.
    atomic = False

    dependencies = [
        ('tenants', '0013_apikey_least_privilege'),
    ]

    operations = [
        migrations.RunPython(
            retire_unsafe_and_duplicate_endpoints,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name='webhookendpoint',
            constraint=models.CheckConstraint(
                condition=(
                    Q(deleted_at__isnull=False)
                    | Q(url__startswith='https://')
                ),
                name='webhook_endpoint_live_https_only',
            ),
        ),
        migrations.AddConstraint(
            model_name='webhookendpoint',
            constraint=models.UniqueConstraint(
                condition=Q(deleted_at__isnull=True),
                fields=('tenant', 'url'),
                name='unique_live_tenant_webhook_url',
            ),
        ),
    ]
