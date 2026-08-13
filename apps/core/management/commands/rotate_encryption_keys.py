from django.core.management.base import BaseCommand
from django.db import transaction

from apps.datasources.encryption import decrypt, decrypt_text, encrypt, encrypt_text


class Command(BaseCommand):
    help = (
        'Перешифровывает credentials, webhook secrets и pending provider '
        'checkpoints текущим primary Fernet-ключом.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        from apps.datasources.models import DataSourceConnection
        from apps.marketplaces.models import MarketplaceAccount
        from apps.media_processing.models import MediaProcessingJob
        from apps.tenants.models import WebhookEndpoint
        from apps.web_research.models import WebSearchAttempt, WebSearchConnection

        dry_run = options['dry_run']
        counts = {
            'datasources': 0,
            'marketplace_accounts': 0,
            'media_provider_checkpoints': 0,
            'web_search_checkpoints': 0,
            'webhook_endpoints': 0,
            'web_search_connections': 0,
        }
        with transaction.atomic():
            for datasource in (
                DataSourceConnection.all_objects.order_by('pk')
                .select_for_update().iterator()
            ):
                credentials = decrypt(datasource.credentials)
                if not dry_run:
                    datasource.credentials = encrypt(credentials)
                    datasource.save(update_fields=['credentials'])
                counts['datasources'] += 1
            for account in (
                MarketplaceAccount.all_objects.order_by('pk')
                .select_for_update().iterator()
            ):
                credentials = decrypt(account.credentials_enc)
                if not dry_run:
                    account.credentials_enc = encrypt(credentials)
                    account.save(update_fields=['credentials_enc'])
                counts['marketplace_accounts'] += 1
            for endpoint in (
                WebhookEndpoint.all_objects.order_by('pk')
                .select_for_update().iterator()
            ):
                secret = decrypt_text(endpoint.secret_encrypted)
                if not dry_run:
                    endpoint.secret_encrypted = encrypt_text(secret)
                    endpoint.save(update_fields=['secret_encrypted'])
                counts['webhook_endpoints'] += 1
            for media_job in (
                MediaProcessingJob.objects.exclude(
                    provider_response_enc__isnull=True,
                ).order_by('pk').select_for_update().iterator()
            ):
                encrypted_checkpoint = media_job.provider_response_enc
                if encrypted_checkpoint is None:
                    continue
                checkpoint = decrypt(bytes(encrypted_checkpoint))
                if not dry_run:
                    media_job.provider_response_enc = encrypt(checkpoint)
                    media_job.save(update_fields=['provider_response_enc'])
                counts['media_provider_checkpoints'] += 1
            for web_search_connection in (
                WebSearchConnection.objects.exclude(
                    credentials_enc__isnull=True,
                ).order_by('pk').select_for_update().iterator()
            ):
                encrypted_credentials = web_search_connection.credentials_enc
                if encrypted_credentials is None:
                    # Defensive against a concurrent credential removal after
                    # the queryset starts streaming.
                    continue
                credentials = decrypt(bytes(encrypted_credentials))
                if not dry_run:
                    web_search_connection.credentials_enc = encrypt(credentials)
                    web_search_connection.save(
                        update_fields=['credentials_enc'],
                    )
                counts['web_search_connections'] += 1
            for attempt in (
                WebSearchAttempt.objects.exclude(checkpoint_enc__isnull=True)
                .order_by('pk').select_for_update().iterator()
            ):
                encrypted_checkpoint = attempt.checkpoint_enc
                if encrypted_checkpoint is None:
                    continue
                checkpoint = decrypt(bytes(encrypted_checkpoint))
                if not dry_run:
                    attempt.checkpoint_enc = encrypt(checkpoint)
                    attempt.save(update_fields=['checkpoint_enc'])
                counts['web_search_checkpoints'] += 1
            if dry_run:
                transaction.set_rollback(True)

        prefix = '[dry-run] ' if dry_run else ''
        for name, count in counts.items():
            self.stdout.write(f'{prefix}{name}: {count}')
