from datetime import timedelta
from unittest.mock import patch

import pytest
from django.test import Client, RequestFactory
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied
from rest_framework.test import APIRequestFactory

from apps.ai_agent.views import AISettingsView
from apps.core.middleware import TenantMiddleware
from apps.image_search.views import ImageApproveView, ImageRejectView
from apps.marketplaces.views import (
    ListingApproveView,
    ListingRefreshBrandCatalogView,
    ListingRegenerateView,
)
from apps.media_processing.views import TenantMediaSettingsView
from apps.products.views import (
    ProductBulkActionView,
    ProductBulkDeleteView,
    ProductArchiveView,
    ProductCatalogClassificationReviewView,
    ProductDetailView,
    ProductEnrichmentFactReviewView,
    ProductFitmentReviewView,
    ProductListView,
    ProductParseView,
    ProductPublishView,
    ProductRegenerateView,
    ProductReviewQueueActionView,
    ProductSyncView,
    TenantCatalogCategoryDefaultImageView,
)
from apps.tenants.authentication import APIKeyAuthentication
from apps.tenants.api_views import CatalogAPIView
from apps.tenants.models import APIKey
from apps.tenants.principals import APIKeyPrincipal
from apps.tenants.serializers import APIKeyCreateSerializer
from apps.tenants.services import APIKeyService, TenantService
from apps.tenants.tests.auth import all_machine_scopes
from apps.tenants.views import TenantDetailView
from apps.users.views import UpdateProfileView
from apps.web_research.views import TenantWebResearchSettingsView


class UnreviewedCatalogView(CatalogAPIView):
    """Test-only view proving inherited scope maps are not an opt-in."""


ENRICHMENT_WORKFLOW_SCOPES = {
    'catalog:write',
    'listings:write',
    'ai:run',
    'research:run',
    'media:write',
}
SYNC_WORKFLOW_SCOPES = {'sync:run', 'catalog:write', 'listings:write'}


def _authentication_request(view_class, tenant, raw_key, method='get'):
    factory = APIRequestFactory()
    request_factory = getattr(factory, method.lower())
    http_request = request_factory(
        '/test/',
        HTTP_AUTHORIZATION=f'Bearer {raw_key}',
    )
    http_request.tenant = tenant
    view = view_class()
    view.args = ()
    view.kwargs = {}
    return view.initialize_request(http_request)


@pytest.mark.django_db
class TestAPIKeyLeastPrivilege:
    def test_auth_returns_machine_principal_not_tenant_owner(self):
        tenant, raw_key = TenantService.create_tenant(
            'Principal Co', 'principal-co', 'owner@principal.test', 'pass12345',
        )
        request = _authentication_request(TenantDetailView, tenant, raw_key)

        principal, api_key = APIKeyAuthentication().authenticate(request)

        assert isinstance(principal, APIKeyPrincipal)
        assert principal.api_key_id == api_key.pk
        assert principal.tenant_id == tenant.pk
        assert not hasattr(principal, 'email')
        assert principal.can_manage_billing() is False
        assert principal.can_manage_users() is False
        assert principal.can_manage_connections() is False

    @patch('apps.users.services.send_mail')
    def test_api_key_cannot_request_owner_email_change(self, send_mail):
        tenant, _ = TenantService.create_tenant(
            'No Takeover', 'no-takeover', 'owner@safe.test', 'pass12345',
        )
        _, raw_key = APIKey.generate(
            tenant,
            'all-scopes',
            role=APIKey.ROLE_OPERATOR,
            scopes=all_machine_scopes(),
        )

        response = Client().post(
            '/api/v1/auth/change-email/',
            {'new_email': 'attacker@example.test'},
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {raw_key}',
        )

        assert response.status_code == 403
        send_mail.assert_not_called()
        assert tenant.members.get(role='owner').user.email == 'owner@safe.test'

    def test_api_key_cannot_manage_api_keys(self):
        tenant, _ = TenantService.create_tenant(
            'No Key Admin', 'no-key-admin', 'owner@keys.test', 'pass12345',
        )
        _, raw_key = APIKey.generate(
            tenant,
            'all-scopes',
            role=APIKey.ROLE_OPERATOR,
            scopes=all_machine_scopes(),
        )
        client = Client(HTTP_AUTHORIZATION=f'Bearer {raw_key}')

        assert client.get('/api/v1/tenant/api-keys/').status_code == 403
        assert client.post(
            '/api/v1/tenant/api-keys/',
            {'name': 'Backdoor'},
            content_type='application/json',
        ).status_code == 403

    def test_raw_api_view_is_default_deny_for_api_keys(self):
        tenant, _ = TenantService.create_tenant(
            'Default Deny', 'default-deny', 'owner@deny.test', 'pass12345',
        )
        _, raw_key = APIKey.generate(
            tenant,
            'all-scopes',
            role=APIKey.ROLE_OPERATOR,
            scopes=all_machine_scopes(),
        )
        request = _authentication_request(UpdateProfileView, tenant, raw_key)

        with pytest.raises(PermissionDenied):
            APIKeyAuthentication().authenticate(request)

    def test_inherited_scope_map_does_not_enable_new_view(self):
        tenant, _ = TenantService.create_tenant(
            'No Implicit Opt-in',
            'no-implicit-opt-in',
            'owner@implicit.test',
            'pass12345',
        )
        _, raw_key = APIKey.generate(
            tenant,
            'all-scopes',
            role=APIKey.ROLE_OPERATOR,
            scopes=all_machine_scopes(),
        )
        request = _authentication_request(
            UnreviewedCatalogView, tenant, raw_key,
        )

        with pytest.raises(PermissionDenied):
            APIKeyAuthentication().authenticate(request)

    @pytest.mark.parametrize('view_class', [
        AISettingsView,
        TenantMediaSettingsView,
        TenantWebResearchSettingsView,
    ])
    def test_tenant_settings_are_human_only(self, view_class):
        tenant, _ = TenantService.create_tenant(
            'Human Settings',
            f'human-settings-{view_class.__name__.lower()}',
            f'{view_class.__name__.lower()}@settings.test',
            'pass12345',
        )
        _, raw_key = APIKey.generate(
            tenant,
            'all-scopes',
            role=APIKey.ROLE_OPERATOR,
            scopes=all_machine_scopes(),
        )
        request = _authentication_request(view_class, tenant, raw_key)

        with pytest.raises(PermissionDenied):
            APIKeyAuthentication().authenticate(request)

    @pytest.mark.parametrize('view_class', [
        ProductReviewQueueActionView,
        ProductFitmentReviewView,
        ProductEnrichmentFactReviewView,
        ProductCatalogClassificationReviewView,
        ListingApproveView,
        ListingRefreshBrandCatalogView,
        ImageApproveView,
        ImageRejectView,
    ])
    def test_human_review_actions_reject_machine_principals(self, view_class):
        tenant, _ = TenantService.create_tenant(
            'Human Review',
            'human-review',
            'owner@review.test',
            'pass12345',
        )
        _, raw_key = APIKey.generate(
            tenant,
            'review-writer',
            role=APIKey.ROLE_OPERATOR,
            scopes=all_machine_scopes(),
        )
        request = _authentication_request(view_class, tenant, raw_key, method='post')

        with pytest.raises(PermissionDenied):
            APIKeyAuthentication().authenticate(request)

    def test_api_key_cannot_cross_tenant_boundary(self):
        tenant_a, _ = TenantService.create_tenant(
            'Boundary A', 'boundary-a', 'a@boundary.test', 'pass12345',
        )
        tenant_b, _ = TenantService.create_tenant(
            'Boundary B', 'boundary-b', 'b@boundary.test', 'pass12345',
        )
        _, raw_key = APIKey.generate(
            tenant_a,
            'reader-a',
            scopes=['catalog:read'],
        )
        request = _authentication_request(ProductListView, tenant_b, raw_key)

        with pytest.raises(PermissionDenied):
            APIKeyAuthentication().authenticate(request)

    def test_missing_scope_is_denied(self):
        tenant, _ = TenantService.create_tenant(
            'Missing Scope', 'missing-scope', 'owner@scope.test', 'pass12345',
        )
        _, raw_key = APIKey.generate(tenant, 'empty', scopes=[])
        request = _authentication_request(TenantDetailView, tenant, raw_key)

        with pytest.raises(PermissionDenied):
            APIKeyAuthentication().authenticate(request)

    def test_viewer_cannot_write_even_with_injected_scope(self):
        tenant, _ = TenantService.create_tenant(
            'Viewer Ceiling', 'viewer-ceiling', 'owner@viewer.test', 'pass12345',
        )
        api_key, raw_key = APIKey.generate(tenant, 'viewer')
        api_key.scopes = ['catalog:write']
        api_key.save(update_fields=['scopes'])
        request = _authentication_request(ProductListView, tenant, raw_key, method='post')

        with pytest.raises(PermissionDenied):
            APIKeyAuthentication().authenticate(request)

    def test_operator_with_required_scope_can_authenticate(self):
        tenant, _ = TenantService.create_tenant(
            'Operator Scope', 'operator-scope', 'owner@operator.test', 'pass12345',
        )
        _, raw_key = APIKey.generate(
            tenant,
            'operator',
            role=APIKey.ROLE_OPERATOR,
            scopes=['catalog:write'],
        )
        request = _authentication_request(ProductListView, tenant, raw_key, method='post')

        principal, _ = APIKeyAuthentication().authenticate(request)

        assert principal.role == APIKey.ROLE_OPERATOR
        assert principal.has_scopes({'catalog:write'})

    @pytest.mark.parametrize(('view_class', 'granted_scopes', 'required_scopes'), [
        (ProductSyncView, ['sync:run'], SYNC_WORKFLOW_SCOPES),
        (ProductPublishView, ['catalog:write'], {'catalog:write', 'listings:write'}),
        (ProductArchiveView, ['catalog:write'], {'catalog:write', 'listings:write'}),
        (ProductParseView, ['catalog:write'], ENRICHMENT_WORKFLOW_SCOPES),
        (ProductRegenerateView, ['catalog:write'], ENRICHMENT_WORKFLOW_SCOPES),
        (ListingRegenerateView, ['listings:write'], ENRICHMENT_WORKFLOW_SCOPES),
        (
            TenantCatalogCategoryDefaultImageView,
            ['catalog:write'],
            {'catalog:write', 'media:write'},
        ),
    ])
    def test_cross_domain_actions_require_dedicated_scopes(
        self, view_class, granted_scopes, required_scopes,
    ):
        tenant, _ = TenantService.create_tenant(
            'Dedicated Scope',
            f'dedicated-{view_class.__name__.lower()}',
            f'{view_class.__name__.lower()}@dedicated.test',
            'pass12345',
        )
        _, raw_key = APIKey.generate(
            tenant,
            'insufficient',
            role=APIKey.ROLE_OPERATOR,
            scopes=granted_scopes,
        )
        request = _authentication_request(view_class, tenant, raw_key, method='post')

        with pytest.raises(PermissionDenied):
            APIKeyAuthentication().authenticate(request)

        _, valid_key = APIKey.generate(
            tenant,
            'sufficient',
            role=APIKey.ROLE_OPERATOR,
            scopes=sorted(required_scopes),
        )
        valid_request = _authentication_request(
            view_class, tenant, valid_key, method='post',
        )

        principal, _ = APIKeyAuthentication().authenticate(valid_request)
        assert principal.has_scopes(required_scopes)

    @pytest.mark.parametrize('view_class', [
        ProductParseView,
        ProductRegenerateView,
        ListingRegenerateView,
    ])
    @pytest.mark.parametrize('missing_scope', sorted(ENRICHMENT_WORKFLOW_SCOPES))
    def test_enrichment_workflow_requires_every_side_effect_scope(
        self, view_class, missing_scope,
    ):
        tenant, _ = TenantService.create_tenant(
            'Workflow Scope',
            'workflow-scope',
            'owner@workflow.test',
            'pass12345',
        )
        _, raw_key = APIKey.generate(
            tenant,
            f'missing-{missing_scope}',
            role=APIKey.ROLE_OPERATOR,
            scopes=sorted(ENRICHMENT_WORKFLOW_SCOPES - {missing_scope}),
        )
        request = _authentication_request(
            view_class, tenant, raw_key, method='post',
        )

        with pytest.raises(PermissionDenied):
            APIKeyAuthentication().authenticate(request)

    @pytest.mark.parametrize('missing_scope', sorted(SYNC_WORKFLOW_SCOPES))
    def test_sync_workflow_requires_every_side_effect_scope(self, missing_scope):
        tenant, _ = TenantService.create_tenant(
            'Sync Scope', 'sync-scope', 'owner@sync.test', 'pass12345',
        )
        _, raw_key = APIKey.generate(
            tenant,
            f'missing-{missing_scope}',
            role=APIKey.ROLE_OPERATOR,
            scopes=sorted(SYNC_WORKFLOW_SCOPES - {missing_scope}),
        )
        request = _authentication_request(
            ProductSyncView, tenant, raw_key, method='post',
        )

        with pytest.raises(PermissionDenied):
            APIKeyAuthentication().authenticate(request)

    @pytest.mark.parametrize(('view_class', 'method', 'granted_scopes', 'required_scopes'), [
        (
            ProductDetailView,
            'patch',
            ['catalog:write'],
            {'catalog:write', 'listings:write'},
        ),
        (
            ProductBulkDeleteView,
            'delete',
            ['catalog:write'],
            {'catalog:write', 'listings:write'},
        ),
    ])
    def test_listing_side_effects_require_listing_scope(
        self, view_class, method, granted_scopes, required_scopes,
    ):
        tenant, _ = TenantService.create_tenant(
            'Listing Side Effect',
            f'side-effect-{view_class.__name__.lower()}',
            f'{view_class.__name__.lower()}@side-effect.test',
            'pass12345',
        )
        _, insufficient = APIKey.generate(
            tenant,
            'catalog-only',
            role=APIKey.ROLE_OPERATOR,
            scopes=granted_scopes,
        )

        with pytest.raises(PermissionDenied):
            APIKeyAuthentication().authenticate(_authentication_request(
                view_class, tenant, insufficient, method=method,
            ))

        _, sufficient = APIKey.generate(
            tenant,
            'catalog-and-listings',
            role=APIKey.ROLE_OPERATOR,
            scopes=sorted(required_scopes),
        )
        principal, _ = APIKeyAuthentication().authenticate(_authentication_request(
            view_class, tenant, sufficient, method=method,
        ))
        assert principal.has_scopes(required_scopes)

    def test_polymorphic_bulk_action_is_machine_default_deny(self):
        tenant, _ = TenantService.create_tenant(
            'Bulk Human', 'bulk-human', 'bulk@human.test', 'pass12345',
        )
        _, raw_key = APIKey.generate(
            tenant,
            'all-machine-scopes',
            role=APIKey.ROLE_OPERATOR,
            scopes=all_machine_scopes(),
        )
        request = _authentication_request(
            ProductBulkActionView, tenant, raw_key, method='post',
        )

        with pytest.raises(PermissionDenied):
            APIKeyAuthentication().authenticate(request)

    def test_expired_and_revoked_keys_are_rejected(self):
        tenant, _ = TenantService.create_tenant(
            'Expiry Co', 'expiry-co', 'owner@expiry.test', 'pass12345',
        )
        api_key, raw_key = APIKey.generate(tenant, 'expiry')
        api_key.expires_at = timezone.now() - timedelta(seconds=1)
        api_key.save(update_fields=['expires_at'])
        assert APIKey.verify(raw_key) is None

        api_key.expires_at = timezone.now() + timedelta(days=1)
        api_key.save(update_fields=['expires_at'])
        APIKeyService.revoke_key(api_key.pk, tenant)
        api_key.refresh_from_db()
        assert api_key.revoked_at is not None
        assert APIKey.verify(raw_key) is None

    def test_invalid_expired_or_revoked_key_never_falls_back_to_host_tenant(self):
        tenant, _ = TenantService.create_tenant(
            'No Host Fallback',
            'no-host-fallback',
            'owner@fallback.test',
            'pass12345',
        )
        revoked_key, revoked_raw = APIKey.generate(tenant, 'revoked')
        APIKeyService.revoke_key(revoked_key.pk, tenant)
        expired_key, expired_raw = APIKey.generate(tenant, 'expired')
        expired_key.expires_at = timezone.now() - timedelta(seconds=1)
        expired_key.save(update_fields=['expires_at'])
        middleware = TenantMiddleware(lambda request: None)
        factory = RequestFactory()

        for raw_key in ('map_sk_invalid', revoked_raw, expired_raw):
            request = factory.get(
                '/api/v1/tenant/',
                HTTP_AUTHORIZATION=f'Bearer {raw_key}',
                HTTP_HOST=f'{tenant.slug}.example.test',
            )

            assert middleware._resolve_tenant(request) is None


@pytest.mark.django_db
class TestAPIKeyCreationPolicy:
    def test_serializer_rejects_viewer_write_scope_and_duplicates(self):
        viewer = APIKeyCreateSerializer(data={
            'name': 'unsafe',
            'role': 'viewer',
            'scopes': ['catalog:write'],
        })
        duplicate = APIKeyCreateSerializer(data={
            'name': 'duplicate',
            'role': 'operator',
            'scopes': ['catalog:read', 'catalog:read'],
        })

        assert not viewer.is_valid()
        assert not duplicate.is_valid()

    def test_serializer_caps_expiry_and_rejects_human_roles(self):
        too_long = APIKeyCreateSerializer(data={
            'name': 'long',
            'role': 'operator',
            'scopes': [],
            'expires_in_days': 366,
        })
        owner = APIKeyCreateSerializer(data={
            'name': 'owner',
            'role': 'owner',
            'scopes': [],
        })

        assert not too_long.is_valid()
        assert not owner.is_valid()
