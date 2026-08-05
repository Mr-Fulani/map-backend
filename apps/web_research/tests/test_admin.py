from decimal import Decimal

import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory

from apps.products.models import Product
from apps.tenants.models import TenantUser
from apps.tenants.services import TenantService
from apps.web_research.admin import (
    WebResearchClaimAdmin, WebResearchEvidenceAdmin, WebResearchRunAdmin,
    WebSearchConnectionAdmin, WebSearchConnectionForm,
)
from apps.web_research.models import (
    WebResearchClaim, WebResearchEvidence, WebResearchRun, WebSearchConnection,
)


def make_tenant(slug):
    tenant, _ = TenantService.create_tenant(
        slug, slug, f'{slug}@test.com', 'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article=f'{slug}-001',
        name=f'Товар {slug}',
        price=Decimal('0'),
    )
    run = WebResearchRun.objects.create(
        tenant=tenant,
        product=product,
        status=WebResearchRun.Status.NEED_REVIEW,
    )
    evidence = WebResearchEvidence.objects.create(
        run=run,
        query='test',
        rank=1,
        title='Test source',
        url=f'https://{slug}.example.com/part',
        domain=f'{slug}.example.com',
    )
    claim = WebResearchClaim.objects.create(
        run=run,
        claim_type=WebResearchClaim.ClaimType.FACT,
        payload={'name': 'test'},
    )
    claim.evidence.add(evidence)
    return tenant, run, evidence, claim


@pytest.mark.django_db
def test_staff_admin_journals_are_scoped_to_tenant_memberships():
    own_tenant, own_run, own_evidence, own_claim = make_tenant('admin-own')
    _, other_run, other_evidence, other_claim = make_tenant('admin-other')
    staff = get_user_model().objects.create_user(
        'research-staff@test.com', 'pass12345', is_staff=True,
    )
    TenantUser.objects.create(
        user=staff,
        tenant=own_tenant,
        role=TenantUser.ROLE_ADMIN,
    )
    request = RequestFactory().get('/admin/web_research/')
    request.user = staff
    site = AdminSite()

    run_admin = WebResearchRunAdmin(WebResearchRun, site)
    evidence_admin = WebResearchEvidenceAdmin(WebResearchEvidence, site)
    claim_admin = WebResearchClaimAdmin(WebResearchClaim, site)

    assert list(run_admin.get_queryset(request)) == [own_run]
    assert list(evidence_admin.get_queryset(request)) == [own_evidence]
    assert list(claim_admin.get_queryset(request)) == [own_claim]
    assert other_run not in run_admin.get_queryset(request)
    assert other_evidence not in evidence_admin.get_queryset(request)
    assert other_claim not in claim_admin.get_queryset(request)


@pytest.mark.django_db
def test_superuser_sees_all_research_but_cannot_mutate_audit_journals():
    make_tenant('admin-super-one')
    make_tenant('admin-super-two')
    superuser = get_user_model().objects.create_superuser(
        'research-super@test.com', 'pass12345',
    )
    request = RequestFactory().get('/admin/web_research/')
    request.user = superuser
    run_admin = WebResearchRunAdmin(WebResearchRun, AdminSite())

    assert run_admin.get_queryset(request).count() == 2
    assert run_admin.has_add_permission(request) is False
    assert run_admin.has_change_permission(request) is False
    assert run_admin.has_delete_permission(request) is False

    client = Client()
    client.force_login(superuser)
    for url in [
        '/admin/web_research/webresearchrun/',
        '/admin/web_research/webresearchevidence/',
        '/admin/web_research/webresearchclaim/',
    ]:
        response = client.get(url)
        assert response.status_code == 200
        assert 'Тенант' in response.content.decode()


@pytest.mark.django_db
def test_connection_form_maps_routing_role_to_priority():
    form = WebSearchConnectionForm(data={
        'provider_id': 'brave',
        'display_name': 'Brave Search',
        'is_active': True,
        'routing_role': 'primary',
        'allowed_plan_slugs': [],
        'parameters': '{}',
        'requests_per_minute': 60,
        'monthly_request_limit': '',
        'api_key': '',
    })

    assert form.is_valid(), form.errors
    connection = form.save()

    assert connection.priority == WebSearchConnection.PRIMARY_PRIORITY


@pytest.mark.django_db
def test_saving_new_primary_demotes_previous_primary():
    previous = WebSearchConnection.objects.create(
        provider_id='brave',
        display_name='Brave Search',
        priority=WebSearchConnection.PRIMARY_PRIORITY,
    )
    replacement = WebSearchConnection(
        provider_id='tavily',
        display_name='Tavily',
        priority=WebSearchConnection.PRIMARY_PRIORITY,
    )
    request = RequestFactory().post('/admin/web_research/websearchconnection/add/')
    request.user = get_user_model().objects.create_superuser(
        'admin@example.com', 'password',
    )
    model_admin = WebSearchConnectionAdmin(WebSearchConnection, AdminSite())

    model_admin.save_model(request, replacement, form=None, change=False)

    previous.refresh_from_db()
    replacement.refresh_from_db()
    assert previous.priority == WebSearchConnection.FALLBACK_PRIORITY
    assert replacement.priority == WebSearchConnection.PRIMARY_PRIORITY
