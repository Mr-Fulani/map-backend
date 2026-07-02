import pytest
from django.core.management import call_command

from apps.products.models import TenantCatalogCategory
from apps.tenants.models import CatalogDomain
from apps.tenants.services import TenantService


def make_tenant(slug):
    tenant, _ = TenantService.create_tenant(slug, slug, f'{slug}@test.com', 'pass12345')
    return tenant


def make_category(tenant, domain, name, external_source='avito', aliases=None):
    return TenantCatalogCategory.objects.create(
        tenant=tenant, name=name, normalized_name=name.lower().replace(' ', ''),
        root_domain=domain, domain=domain.slug,
        external_source=external_source, aliases=aliases or [], is_active=True,
    )


@pytest.mark.django_db
class TestBackfillAvitoCategoryAliases:
    def test_fills_aliases_for_known_seed_name(self):
        tenant = make_tenant('backfill-known-co')
        domain = CatalogDomain.objects.filter(slug='auto_parts').first()
        category = make_category(tenant, domain, 'Тормозная система')

        call_command('backfill_avito_category_aliases')

        category.refresh_from_db()
        assert category.aliases == ['Brakes', 'Тормоза', 'Тормоз']

    def test_leaves_unknown_name_untouched(self):
        tenant = make_tenant('backfill-unknown-co')
        domain = CatalogDomain.objects.filter(slug='auto_parts').first()
        category = make_category(tenant, domain, 'Название, которого нет в старом дереве')

        call_command('backfill_avito_category_aliases')

        category.refresh_from_db()
        assert category.aliases == []

    def test_does_not_overwrite_existing_aliases(self):
        tenant = make_tenant('backfill-existing-co')
        domain = CatalogDomain.objects.filter(slug='auto_parts').first()
        category = make_category(tenant, domain, 'Тормозная система', aliases=['Уже заполнено'])

        call_command('backfill_avito_category_aliases')

        category.refresh_from_db()
        assert category.aliases == ['Уже заполнено']

    def test_dry_run_changes_nothing(self):
        tenant = make_tenant('backfill-dryrun-co')
        domain = CatalogDomain.objects.filter(slug='auto_parts').first()
        category = make_category(tenant, domain, 'Тормозная система')

        call_command('backfill_avito_category_aliases', dry_run=True)

        category.refresh_from_db()
        assert category.aliases == []

    def test_leaves_non_avito_source_untouched(self):
        tenant = make_tenant('backfill-other-source-co')
        domain = CatalogDomain.objects.filter(slug='auto_parts').first()
        category = make_category(tenant, domain, 'Тормозная система', external_source='platform_auto_parts_seed')

        call_command('backfill_avito_category_aliases')

        category.refresh_from_db()
        assert category.aliases == []
