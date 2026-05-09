import datetime

from django.db.models import Sum
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.datasources.encryption import encrypt
from apps.marketplaces.models import CategoryMapping, Listing, ListingStats, MarketplaceAccount
from apps.marketplaces.serializers import (
    CategoryMappingSerializer,
    CategoryMappingWriteSerializer,
    ListingSerializer,
    MarketplaceAccountSerializer,
    MarketplaceAccountWriteSerializer,
)
from apps.core.pagination import MapPagination
from apps.marketplaces.services import CategoryMappingService


class MarketplaceAccountListView(APIView):
    """GET /api/v1/accounts/ — список аккаунтов. POST — создать."""

    def get(self, request):
        """Возвращает аккаунты маркетплейсов текущего тенанта."""
        qs = MarketplaceAccount.objects.filter(tenant=request.tenant)
        return Response(MarketplaceAccountSerializer(qs, many=True).data)

    def post(self, request):
        """
        Создаёт аккаунт Avito с зашифрованными client_id/client_secret.

        Поля client_id и client_secret шифруются Fernet и не возвращаются в ответе.
        """
        serializer = MarketplaceAccountWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            account = MarketplaceAccount.objects.create(
                tenant=request.tenant,
                name=data['name'],
                marketplace=data['marketplace'],
                external_id=data['external_id'],
                credentials_enc=encrypt({
                    'client_id': data['client_id'],
                    'client_secret': data['client_secret'],
                }),
            )
        except Exception:
            return Response(
                {'detail': 'Аккаунт с таким external_id уже существует.'},
                status=status.HTTP_409_CONFLICT,
            )

        return Response(
            MarketplaceAccountSerializer(account).data,
            status=status.HTTP_201_CREATED,
        )


class MarketplaceAccountDetailView(APIView):
    """GET/PUT/DELETE /api/v1/accounts/{id}/"""

    def _get_account(self, pk, tenant):
        """Возвращает аккаунт тенанта или 404."""
        try:
            return MarketplaceAccount.objects.get(pk=pk, tenant=tenant)
        except MarketplaceAccount.DoesNotExist:
            return None

    def get(self, request, pk):
        """Детали аккаунта."""
        account = self._get_account(pk, request.tenant)
        if account is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(MarketplaceAccountSerializer(account).data)

    def put(self, request, pk):
        """
        Обновляет аккаунт.

        Если переданы client_id/client_secret — перешифровывает credentials.
        """
        account = self._get_account(pk, request.tenant)
        if account is None:
            return Response(status=status.HTTP_404_NOT_FOUND)

        serializer = MarketplaceAccountWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        account.name = data['name']
        account.marketplace = data['marketplace']
        account.external_id = data['external_id']
        account.credentials_enc = encrypt({
            'client_id': data['client_id'],
            'client_secret': data['client_secret'],
        })
        account.save(update_fields=['name', 'marketplace', 'external_id', 'credentials_enc'])

        return Response(MarketplaceAccountSerializer(account).data)

    def delete(self, request, pk):
        """Удаляет аккаунт вместе со всеми его листингами."""
        account = self._get_account(pk, request.tenant)
        if account is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        account.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class UnmappedCategoriesView(APIView):
    def get(self, request):
        categories = CategoryMappingService.get_unmapped_categories(request.tenant)
        return Response({'unmapped': categories, 'count': len(categories)})


class CategoryMappingListView(APIView):
    def get(self, request):
        qs = CategoryMapping.objects.filter(tenant=request.tenant)
        return Response(CategoryMappingSerializer(qs, many=True).data)

    def post(self, request):
        serializer = CategoryMappingWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        mapping, _ = CategoryMapping.objects.update_or_create(
            tenant=request.tenant,
            marketplace=CategoryMapping.MARKETPLACE_AVITO,
            category_source=data['category_source'],
            defaults={
                'category_target': data['category_target'],
                'category_id': data['category_id'],
                'attributes_map': data.get('attributes_map', {}),
            },
        )
        return Response(CategoryMappingSerializer(mapping).data, status=status.HTTP_201_CREATED)


class CategoryMappingDetailView(APIView):
    def get(self, request, pk):
        mapping = CategoryMapping.objects.get(pk=pk, tenant=request.tenant)
        return Response(CategoryMappingSerializer(mapping).data)

    def put(self, request, pk):
        mapping = CategoryMapping.objects.get(pk=pk, tenant=request.tenant)
        serializer = CategoryMappingWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        for field, value in data.items():
            setattr(mapping, field, value)
        mapping.version += 1
        mapping.save()
        return Response(CategoryMappingSerializer(mapping).data)

    def delete(self, request, pk):
        CategoryMapping.objects.filter(pk=pk, tenant=request.tenant).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ListingListView(APIView):
    """
    GET /api/v1/listings/ — листинги тенанта с фильтром по статусу и пагинацией.

    Query params:
        status   — draft | pending | active | rejected | archived | requires_review
        account  — id аккаунта MarketplaceAccount
    """

    def get(self, request):
        """Возвращает страницу листингов текущего тенанта."""
        qs = (
            Listing.objects.filter(tenant=request.tenant)
            .select_related('product', 'account')
            .order_by('-created_at')
        )

        listing_status = request.query_params.get('status', '').strip()
        if listing_status:
            qs = qs.filter(status=listing_status)

        account_id = request.query_params.get('account', '').strip()
        if account_id:
            qs = qs.filter(account_id=account_id)

        paginator = MapPagination()
        page = paginator.paginate_queryset(qs, request)
        return paginator.get_paginated_response(ListingSerializer(page, many=True).data)


class AnalyticsView(APIView):
    """
    GET /api/v1/analytics/ — агрегированная статистика листингов тенанта.

    Query params:
        date_from — YYYY-MM-DD (по умолчанию 30 дней назад)
        date_to   — YYYY-MM-DD (по умолчанию сегодня)
    """

    def get(self, request):
        """Возвращает сводку и помесячную/ежедневную статистику просмотров."""
        today = datetime.date.today()
        date_from_str = request.query_params.get('date_from', '')
        date_to_str = request.query_params.get('date_to', '')

        try:
            date_from = (
                datetime.date.fromisoformat(date_from_str)
                if date_from_str else today - datetime.timedelta(days=29)
            )
            date_to = (
                datetime.date.fromisoformat(date_to_str)
                if date_to_str else today
            )
        except ValueError:
            return Response(
                {'status': 'error', 'code': 'invalid_date',
                 'detail': 'Формат даты: YYYY-MM-DD'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        qs = ListingStats.objects.filter(
            tenant=request.tenant,
            date__gte=date_from,
            date__lte=date_to,
        )

        totals = qs.aggregate(
            total_views=Sum('views'),
            total_contacts=Sum('contacts'),
            total_impressions=Sum('impressions'),
        )
        total_views = totals['total_views'] or 0
        total_contacts = totals['total_contacts'] or 0
        total_impressions = totals['total_impressions'] or 0
        avg_ctr = round(total_views / total_impressions * 100, 2) if total_impressions else 0.0

        # Активные листинги тенанта
        active_listings = Listing.objects.filter(
            tenant=request.tenant, status=Listing.STATUS_ACTIVE,
        ).count()

        # Дневные точки для графика
        daily = list(
            qs.values('date')
            .annotate(
                views=Sum('views'),
                contacts=Sum('contacts'),
                impressions=Sum('impressions'),
            )
            .order_by('date')
            .values('date', 'views', 'contacts', 'impressions')
        )
        for row in daily:
            row['date'] = str(row['date'])

        return Response({
            'status': 'ok',
            'data': {
                'summary': {
                    'views': total_views,
                    'contacts': total_contacts,
                    'impressions': total_impressions,
                    'avg_ctr': avg_ctr,
                    'active_listings': active_listings,
                },
                'daily': daily,
                'date_from': str(date_from),
                'date_to': str(date_to),
            },
        })
