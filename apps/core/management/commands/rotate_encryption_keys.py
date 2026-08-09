from django.core.management.base import BaseCommand
from django.db import transaction

from apps.datasources.encryption import decrypt, decrypt_text, encrypt, encrypt_text


class Command(BaseCommand):
    help = 'Перешифровывает credentials и webhook secrets текущим primary Fernet-ключом.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        from apps.datasources.models import DataSourceConnection
        from apps.marketplaces.models import MarketplaceAccount
        from apps.tenants.models import WebhookEndpoint
        from apps.web_research.models import WebSearchConnection

        dry_run = options['dry_run']
        counts = {
            'datasources': 0,
            'marketplace_accounts': 0,
            'webhook_endpoints': 0,
            'web_search_connections': 0,
        }
        with transaction.atomic():
            for connection in DataSourceConnection.all_objects.iterator():
                plaintext = decrypt(connection.credentials)
                if not dry_run:
                    connection.credentials = encrypt(plaintext)
                    connection.save(update_fields=['credentials', 'updated_at'])
                counts['datasources'] += 1
            for account in MarketplaceAccount.all_objects.iterator():
                plaintext = decrypt(account.credentials_enc)
                if not dry_run:
                    account.credentials_enc = encrypt(plaintext)
                    account.save(update_fields=['credentials_enc', 'updated_at'])
                counts['marketplace_accounts'] += 1
            for endpoint in WebhookEndpoint.all_objects.iterator():
                plaintext = decrypt_text(endpoint.secret_encrypted)
                if not dry_run:
                    endpoint.secret_encrypted = encrypt_text(plaintext)
                    endpoint.save(update_fields=['secret_encrypted', 'updated_at'])
                counts['webhook_endpoints'] += 1
            for connection in WebSearchConnection.objects.exclude(
                credentials_enc__isnull=True,
            ).iterator():
                plaintext = decrypt(connection.credentials_enc)
                if not dry_run:
                    connection.credentials_enc = encrypt(plaintext)
                    connection.save(update_fields=['credentials_enc', 'updated_at'])
                counts['web_search_connections'] += 1
            if dry_run:
                transaction.set_rollback(True)

        prefix = '[dry-run] ' if dry_run else ''
        for name, count in counts.items():
            self.stdout.write(f'{prefix}{name}: {count}')
