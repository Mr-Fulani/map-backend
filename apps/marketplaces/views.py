import datetime

from django.db.models import Sum
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.marketplaces.models import CategoryMapping, Listing, ListingStats, MarketplaceAccount
from apps.marketplaces.serializers import (
    CategoryMappingSerializer,
    CategoryMappingWriteSerializer,
    ListingDetailSerializer,
    ListingSerializer,
    MarketplaceAccountSerializer,
    MarketplaceAccountWriteSerializer,
)
from apps.core.pagination import MapPagination
from apps.marketplaces.services import (
    AccountAlreadyExists,
    CategoryMappingService,
    InvalidMarketplaceCredentials,
    InvalidListingStatus,
    ListingNotFound,
    ListingService,
    MarketplaceAccountService,
)


@extend_schema(tags=['Accounts'])
class MarketplaceAccountListView(APIView):
    """GET /api/v1/accounts/ — список аккаунтов. POST — создать."""

    def get(self, request):
        """Возвращает аккаунты маркетплейсов текущего тенанта."""
        qs = MarketplaceAccount.objects.filter(tenant=request.tenant)
        return Response(MarketplaceAccountSerializer(qs, many=True).data)

    def post(self, request):
        """Создаёт аккаунт Avito, делегируя логику MarketplaceAccountService.create."""
        serializer = MarketplaceAccountWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            account = MarketplaceAccountService.create(request.tenant, serializer.validated_data)
        except AccountAlreadyExists as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_409_CONFLICT)
        except InvalidMarketplaceCredentials as exc:
            return Response({
                'status': 'error',
                'code': 'validation_error',
                'message': str(exc),
            }, status=status.HTTP_400_BAD_REQUEST)
        return Response(MarketplaceAccountSerializer(account).data, status=status.HTTP_201_CREATED)


@extend_schema(tags=['Accounts'])
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
        """Обновляет аккаунт, делегируя логику MarketplaceAccountService.update_credentials."""
        account = self._get_account(pk, request.tenant)
        if account is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        serializer = MarketplaceAccountWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            account = MarketplaceAccountService.update_credentials(account, serializer.validated_data)
        except AccountAlreadyExists as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_409_CONFLICT)
        except InvalidMarketplaceCredentials as exc:
            return Response({
                'status': 'error',
                'code': 'validation_error',
                'message': str(exc),
            }, status=status.HTTP_400_BAD_REQUEST)
        return Response(MarketplaceAccountSerializer(account).data)

    def patch(self, request, pk):
        """Частичное обновление аккаунта, делегируя логику MarketplaceAccountService.update_partial."""
        account = self._get_account(pk, request.tenant)
        if account is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        account = MarketplaceAccountService.update_partial(account, request.data)
        return Response(MarketplaceAccountSerializer(account).data)

    def delete(self, request, pk):
        """Удаляет аккаунт вместе со всеми его листингами."""
        account = self._get_account(pk, request.tenant)
        if account is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        account.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Accounts'])
class AutoloadStatusView(APIView):
    """GET /api/v1/accounts/{id}/autoload-status/ — проверить активирована ли Автозагрузка Avito."""

    def get(self, request, pk):
        """
        Проверяет активирован ли Avito Autoload для аккаунта.

        Возвращает:
          {"activated": true, "feed_url": "https://..."}  — если профиль найден
          {"activated": false, "feed_url": "https://...", "activate_url": "https://www.avito.ru/autoload/settings"}
        """
        try:
            account = MarketplaceAccount.objects.get(pk=pk, tenant=request.tenant)
        except MarketplaceAccount.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)

        from apps.marketplaces.adapters.avito.adapter import AvitoAdapter
        import requests as req
        from apps.marketplaces.adapters.avito.auth import AvitoAuthManager

        adapter = AvitoAdapter(account)
        feed_url = adapter._feed_public_url()

        try:
            token = AvitoAuthManager().get_token(account)
            resp = req.get(
                'https://api.avito.ru/autoload/v2/profile',
                headers={'Authorization': f'Bearer {token}'},
                timeout=10,
            )
            activated = resp.status_code == 200
        except Exception:
            activated = False

        payload = {'activated': activated, 'feed_url': feed_url}
        if not activated:
            payload['activate_url'] = 'https://www.avito.ru/autoload/settings'
        return Response(payload)


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


@extend_schema(tags=['Listings'])
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
            Listing.objects.filter(tenant=request.tenant, account__is_active=True)
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


@extend_schema(tags=['Listings'])
class ListingDetailView(APIView):
    """
    GET  /api/v1/listings/{id}/ — детали листинга с AI-полями и фотографиями.
    PATCH /api/v1/listings/{id}/ — обновить title / description_ai.
    """

    def get(self, request, pk):
        """Возвращает полные данные листинга включая AI-описание и изображения товара."""
        try:
            listing = ListingService.get_for_tenant(pk, request.tenant)
        except ListingNotFound:
            return Response({'status': 'error', 'code': 'not_found'}, status=status.HTTP_404_NOT_FOUND)
        return Response({'status': 'ok', 'data': ListingDetailSerializer(listing, context={'request': request}).data})

    def patch(self, request, pk):
        """
        Обновляет заголовок и/или AI-описание листинга.

        Доступно в любом статусе кроме active и deleted.
        """
        title = request.data.get('title')
        description_ai = request.data.get('description_ai')
        try:
            listing = ListingService.update_content(pk, request.tenant, title, description_ai)
        except ListingNotFound:
            return Response({'status': 'error', 'code': 'not_found'}, status=status.HTTP_404_NOT_FOUND)
        except InvalidListingStatus as exc:
            return Response({'status': 'error', 'code': 'invalid_status', 'message': str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response({'status': 'ok', 'data': ListingDetailSerializer(listing, context={'request': request}).data})


@extend_schema(tags=['Listings'])
class ListingApproveView(APIView):
    """POST /api/v1/listings/{id}/approve/ — одобрить листинг requires_review и опубликовать."""

    def post(self, request, pk):
        """Одобряет листинг и ставит задачу публикации в Celery."""
        try:
            listing = ListingService.approve(pk, request.tenant)
        except ListingNotFound:
            return Response({'status': 'error', 'code': 'not_found'}, status=status.HTTP_404_NOT_FOUND)
        except InvalidListingStatus as exc:
            return Response({'status': 'error', 'code': 'invalid_status', 'message': str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response({'status': 'ok', 'data': ListingDetailSerializer(listing, context={'request': request}).data})


@extend_schema(tags=['Listings'])
class ListingPublishView(APIView):
    """POST /api/v1/listings/{id}/publish/ — опубликовать черновик/отклонённый/архивный листинг."""

    def post(self, request, pk):
        """Ставит задачу публикации листинга в Celery."""
        try:
            listing = ListingService.publish(pk, request.tenant)
        except ListingNotFound:
            return Response({'status': 'error', 'code': 'not_found'}, status=status.HTTP_404_NOT_FOUND)
        except InvalidListingStatus as exc:
            return Response({'status': 'error', 'code': 'invalid_status', 'message': str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response({'status': 'ok', 'data': ListingDetailSerializer(listing, context={'request': request}).data})


@extend_schema(tags=['Listings'])
class ListingRegenerateView(APIView):
    """POST /api/v1/listings/{id}/regenerate/ — перегенерировать AI-описание."""

    def post(self, request, pk):
        """Ставит задачу генерации AI-описания для товара в очередь Celery."""
        from apps.billing.services import LimitChecker
        can, reason = LimitChecker().can_generate_ai(request.tenant)
        if not can:
            return Response(
                {'status': 'error', 'code': 'quota_exceeded', 'message': reason},
                status=status.HTTP_402_PAYMENT_REQUIRED,
            )
        try:
            ListingService.request_regenerate(pk, request.tenant)
        except ListingNotFound:
            return Response({'status': 'error', 'code': 'not_found'}, status=status.HTTP_404_NOT_FOUND)
        except InvalidListingStatus as exc:
            return Response({'status': 'error', 'code': 'invalid_status', 'message': str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response({'status': 'ok', 'message': 'Задача генерации поставлена в очередь'})


@extend_schema(tags=['Analytics'])
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
