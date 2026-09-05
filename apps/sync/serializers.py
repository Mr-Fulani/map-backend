from rest_framework import serializers

from apps.sync.models import SyncLog


class SyncLogSerializer(serializers.ModelSerializer):
    """Сериализует запись SyncLog для публичного API тенанта."""

    marketplace = serializers.SerializerMethodField()
    account_id = serializers.SerializerMethodField()
    account_name = serializers.SerializerMethodField()
    operation = serializers.CharField(source='event_type', read_only=True)
    provider_result = serializers.CharField(source='status', read_only=True)
    target_url = serializers.SerializerMethodField()

    class Meta:
        model = SyncLog
        fields = [
            'id', 'event_type', 'status', 'message',
            'operation', 'provider_result',
            'marketplace', 'account_id', 'account_name',
            'payload', 'created_at', 'product', 'listing', 'target_url',
        ]
        read_only_fields = fields

    def get_marketplace(self, obj) -> str | None:
        account = self._account(obj)
        return account.marketplace if account is not None else None

    def get_account_id(self, obj) -> int | None:
        account = self._account(obj)
        return account.pk if account is not None else None

    def get_account_name(self, obj) -> str | None:
        account = self._account(obj)
        return account.name if account is not None else None

    def get_target_url(self, obj) -> str | None:
        account = self._account(obj)
        product = getattr(obj, 'product', None)
        if account is not None and account.marketplace == 'ozon':
            if product is not None and product.tenant_id == obj.tenant_id:
                return f'/dashboard/listings?product={product.pk}&target={account.pk}'
            return '/dashboard/settings#marketplaces'
        listing = getattr(obj, 'listing', None)
        if listing is not None and listing.tenant_id == obj.tenant_id and account is not None:
            return f'/dashboard/listings?listing={listing.pk}'
        if product is not None and product.tenant_id == obj.tenant_id:
            return f'/dashboard/products/{product.pk}'
        return None

    @staticmethod
    def _account(obj):
        if obj.account_id is not None:
            account = obj.account
            return account if account is not None and account.tenant_id == obj.tenant_id else None
        listing = getattr(obj, 'listing', None)
        account = getattr(listing, 'account', None) if listing is not None else None
        if (
            listing is None
            or account is None
            or listing.tenant_id != obj.tenant_id
            or account.tenant_id != obj.tenant_id
        ):
            return None
        return account
