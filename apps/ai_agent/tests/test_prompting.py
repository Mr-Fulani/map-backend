import pytest
from django.core.exceptions import ValidationError

from apps.ai_agent.models import AIPromptTemplate, AITaskType
from apps.ai_agent.prompting import resolve_description_prompt
from apps.ai_agent.prompts import DESCRIPTION_OUTPUT_SCHEMA
from apps.products.models import Product
from apps.tenants.models import Tenant
from apps.tenants.services import TenantService


@pytest.fixture
def prompt_product(db):
    tenant, _ = TenantService.create_tenant(
        'Prompt Tenant', 'prompt-tenant', 'prompt@test.com', 'pass12345',
    )
    return Product.objects.create(
        tenant=tenant,
        article='P50136',
        brand='BREMBO',
        name='Колодки тормозные',
        price='1000.00',
    )


@pytest.mark.django_db
def test_prompt_router_uses_generic_template_for_unknown_domain(prompt_product):
    selection = resolve_description_prompt(prompt_product)

    assert selection.template is not None
    assert selection.template.catalog_domain == ''
    assert selection.template.marketplace == 'avito'


@pytest.mark.django_db
def test_prompt_router_prefers_exact_catalog_domain(prompt_product):
    prompt_product.tenant.catalog_domain = Tenant.CatalogDomain.AUTO_PARTS
    prompt_product.tenant.save(update_fields=['catalog_domain'])

    selection = resolve_description_prompt(prompt_product)

    assert selection.template is not None
    assert selection.template.catalog_domain == 'auto_parts'
    assert selection.version == 'db-v6'
    assert 'Номера для поиска и проверки совместимости' in selection.system_prompt
    assert 'fitment_presentation' in selection.system_prompt
    assert 'content_profile' in selection.system_prompt
    assert 'rich:' in selection.system_prompt


@pytest.mark.django_db
def test_prompt_versions_are_immutable(prompt_product):
    template = AIPromptTemplate.objects.get(
        task_type=AITaskType.DESCRIPTION,
        catalog_domain='',
        marketplace='avito',
        is_active=True,
    )
    template.system_prompt = 'Изменённый текст'

    with pytest.raises(ValidationError, match='неизменяема'):
        template.save()


@pytest.mark.django_db
def test_new_version_can_replace_active_template(prompt_product):
    AIPromptTemplate.objects.filter(
        task_type=AITaskType.DESCRIPTION,
        catalog_domain='',
        marketplace='avito',
    ).update(is_active=False)
    new_template = AIPromptTemplate.objects.create(
        task_type=AITaskType.DESCRIPTION,
        catalog_domain='',
        marketplace='avito',
        version=2,
        name='Generic v2',
        system_prompt='Возвращай точный JSON карточки товара.',
        output_schema=DESCRIPTION_OUTPUT_SCHEMA,
        is_active=True,
    )

    selection = resolve_description_prompt(prompt_product)

    assert selection.template == new_template
    assert selection.version == 'db-v2'
