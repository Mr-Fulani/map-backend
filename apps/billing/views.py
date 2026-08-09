import ipaddress
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.decorators import method_decorator
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.billing.models import (
    AICreditPackage, BillingWebhookEvent, Invoice, Plan,
)
from apps.billing.serializers import (
    AICreditPackageSerializer, AITopupCheckoutSerializer, CheckoutSerializer,
    InvoiceSerializer, PlanSerializer, SubscriptionSerializer,
)
from apps.billing.services import (
    BillingService, CheckoutConflictError, CheckoutKeyLimitError,
    CheckoutManualReviewError, CheckoutPendingError, CheckoutTerminalError,
    LimitChecker,
)
from apps.billing.webhook import is_yookassa_ip
from apps.billing.webhook_processing import (  # noqa: F401
    FINAL_WEBHOOK_DECISIONS as _FINAL_WEBHOOK_DECISIONS,
    SUPPORTED_YOOKASSA_EVENTS as _SUPPORTED_YOOKASSA_EVENTS,
    claim_webhook_event as _claim_webhook_event,
    finalize_webhook_event as _finalize_webhook_event,
    process_claimed_yookassa_event,
)
from apps.tenants.permissions import TenantOwnerPermission


_PLAN_LIST_RESPONSE = inline_serializer(
    name='BillingPlanListResponse',
    fields={
        'status': serializers.CharField(),
        'data': PlanSerializer(many=True),
    },
)
_SUBSCRIPTION_RESPONSE = inline_serializer(
    name='BillingSubscriptionResponse',
    fields={
        'status': serializers.CharField(),
        'data': SubscriptionSerializer(allow_null=True),
    },
)
_USAGE_RESPONSE = inline_serializer(
    name='BillingUsageResponse',
    fields={
        'status': serializers.CharField(),
        'data': serializers.DictField(
            child=serializers.JSONField(),
            help_text='Текущее использование лимитов тарифа и AI-баланса.',
        ),
    },
)
_INVOICE_LIST_RESPONSE = inline_serializer(
    name='BillingInvoiceListResponse',
    fields={
        'status': serializers.CharField(),
        'data': InvoiceSerializer(many=True),
    },
)
_AI_PACKAGE_LIST_RESPONSE = inline_serializer(
    name='BillingAICreditPackageListResponse',
    fields={
        'status': serializers.CharField(),
        'data': AICreditPackageSerializer(many=True),
    },
)
_PAYMENT_URL_RESPONSE = inline_serializer(
    name='BillingPaymentUrlResponse',
    fields={
        'status': serializers.CharField(),
        'data': inline_serializer(
            name='BillingPaymentUrlData',
            fields={'payment_url': serializers.URLField()},
        ),
    },
)


def _checkout_error_response(exc):
    if isinstance(exc, CheckoutConflictError):
        return Response(
            {
                'status': 'error',
                'code': 'idempotency_conflict',
                'message': str(exc),
                'data': {'invoice_id': exc.invoice_id},
            },
            status=status.HTTP_409_CONFLICT,
        )
    if isinstance(exc, CheckoutKeyLimitError):
        return Response(
            {
                'status': 'error',
                'code': 'checkout_key_limit',
                'message': str(exc),
                'data': {
                    'invoice_id': exc.invoice_id,
                    'retryable': False,
                    'reuse_idempotency_key': True,
                },
            },
            status=status.HTTP_409_CONFLICT,
        )
    if isinstance(exc, CheckoutManualReviewError):
        return Response(
            {
                'status': 'error',
                'code': 'checkout_manual_review',
                'message': str(exc),
                'data': {'invoice_id': exc.invoice_id},
            },
            status=status.HTTP_409_CONFLICT,
        )
    if isinstance(exc, CheckoutTerminalError):
        return Response(
            {
                'status': 'error',
                'code': 'checkout_terminal',
                'message': str(exc),
                'data': {
                    'invoice_id': exc.invoice_id,
                    'invoice_status': exc.invoice_status,
                    'retryable': False,
                    'rotate_idempotency_key': True,
                },
            },
            status=status.HTTP_409_CONFLICT,
        )
    response = Response(
        {
            'status': 'error',
            'code': 'checkout_pending',
            'message': str(exc),
            'data': {
                'invoice_id': exc.invoice_id,
                'retryable': True,
            },
        },
        status=status.HTTP_503_SERVICE_UNAVAILABLE,
    )
    response['Retry-After'] = str(exc.retry_after)
    return response


def _webhook_source_ip(request) -> str | None:
    forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    candidate = (
        forwarded_for.split(',')[-1].strip()
        if forwarded_for
        else request.META.get('REMOTE_ADDR', '')
    )
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def _sanitize_webhook_payload(event: str, obj: dict) -> dict:
    """Не сохраняет платёжные реквизиты и другие лишние персональные данные."""
    allowed = {
        key: obj[key]
        for key in (
            'id', 'status', 'payment_id', 'amount',
            'created_at', 'cancellation_details',
        )
        if key in obj
    }
    return {'type': 'notification', 'event': event, 'object': allowed}


def _parse_webhook_amount(obj: dict) -> tuple[Decimal | None, str]:
    amount_obj = obj.get('amount') or {}
    try:
        raw_amount = Decimal(str(amount_obj.get('value')))
        amount = raw_amount.quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError, ValueError):
        return None, ''
    currency = str(amount_obj.get('currency') or '').upper()
    if (
        not raw_amount.is_finite()
        or raw_amount <= 0
        or raw_amount != amount
        or amount > Decimal('99999999.99')
        or len(currency) != 3
        or not currency.isalpha()
    ):
        return None, ''
    return amount, currency


def _safe_webhook_text(value, max_length: int) -> str:
    return value[:max_length] if isinstance(value, str) else ''


def _retry_webhook_response(code: str):
    response = Response(
        {'status': 'error', 'code': code},
        status=status.HTTP_503_SERVICE_UNAVAILABLE,
    )
    response['Retry-After'] = str(settings.YOOKASSA_WEBHOOK_RETRY_AFTER_SECONDS)
    return response


@extend_schema(tags=['Billing'])
class PlanListView(APIView):
    """GET /api/v1/billing/plans/ — список доступных тарифов."""

    permission_classes = [AllowAny]

    @extend_schema(responses=_PLAN_LIST_RESPONSE)
    def get(self, request):
        plans = Plan.objects.filter(is_active=True)
        return Response({
            'status': 'ok',
            'data': PlanSerializer(plans, many=True).data,
        })


@extend_schema(tags=['Billing'])
class SubscriptionView(APIView):
    """GET /api/v1/billing/subscription/ — текущая подписка тенанта."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=_SUBSCRIPTION_RESPONSE)
    def get(self, request):
        try:
            sub = request.tenant.subscription
        except Exception:
            return Response({'status': 'ok', 'data': None})

        return Response({
            'status': 'ok',
            'data': SubscriptionSerializer(sub).data,
        })


@extend_schema(tags=['Billing'])
class UsageView(APIView):
    """GET /api/v1/billing/usage/ — использование лимитов тенантом."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=_USAGE_RESPONSE)
    def get(self, request):
        summary = LimitChecker().get_usage_summary(request.tenant)
        return Response({'status': 'ok', 'data': summary})


@extend_schema(tags=['Billing'])
class InvoiceListView(APIView):
    """GET /api/v1/billing/invoices/ — история платежей тенанта."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=_INVOICE_LIST_RESPONSE)
    def get(self, request):
        invoices = Invoice.objects.filter(tenant=request.tenant).order_by('-created_at')
        return Response({
            'status': 'ok',
            'data': InvoiceSerializer(invoices, many=True).data,
        })


@extend_schema(tags=['Billing'])
class AICreditPackageListView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary='Список пакетов AI-кредитов',
        responses=_AI_PACKAGE_LIST_RESPONSE,
    )
    def get(self, request):
        packages = AICreditPackage.objects.filter(is_active=True)
        return Response({
            'status': 'ok',
            'data': AICreditPackageSerializer(packages, many=True).data,
        })


@method_decorator(transaction.non_atomic_requests, name='dispatch')
@extend_schema(tags=['Billing'])
class AITopupCheckoutView(APIView):
    permission_classes = [IsAuthenticated, TenantOwnerPermission]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'billing_checkout'

    @extend_schema(
        summary='Создать платёж на пополнение AI-кредитов',
        request=AITopupCheckoutSerializer,
        responses={200: _PAYMENT_URL_RESPONSE},
    )
    def post(self, request):
        serializer = AITopupCheckoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return_url = serializer.validated_data.get(
            'return_url',
            f'{settings.BILLING_RETURN_URL_ALLOWED_ORIGINS[0]}'
            '/dashboard/billing?topup=success',
        )
        try:
            confirmation_url = BillingService.create_ai_topup_payment(
                tenant=request.tenant,
                package_id=serializer.validated_data['package_id'],
                return_url=return_url,
                idempotency_key=serializer.validated_data['idempotency_key'],
            )
        except AICreditPackage.DoesNotExist:
            return Response(
                {'status': 'error', 'code': 'package_not_found'},
                status=status.HTTP_404_NOT_FOUND,
            )
        except (
            CheckoutConflictError,
            CheckoutKeyLimitError,
            CheckoutManualReviewError,
            CheckoutPendingError,
            CheckoutTerminalError,
        ) as exc:
            return _checkout_error_response(exc)
        return Response({'status': 'ok', 'data': {'payment_url': confirmation_url}})


@method_decorator(transaction.non_atomic_requests, name='dispatch')
@extend_schema(tags=['Billing'])
class CheckoutView(APIView):
    """
    POST /api/v1/billing/checkout/ — создать платёж YooKassa.

    Возвращает payment_url для редиректа пользователя на страницу оплаты.
    """

    permission_classes = [IsAuthenticated, TenantOwnerPermission]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'billing_checkout'

    @extend_schema(
        request=CheckoutSerializer,
        responses={200: _PAYMENT_URL_RESPONSE},
    )
    def post(self, request):
        serializer = CheckoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        plan_slug = serializer.validated_data['plan_slug']
        period = serializer.validated_data['period']
        return_url = serializer.validated_data.get(
            'return_url',
            f'{settings.BILLING_RETURN_URL_ALLOWED_ORIGINS[0]}'
            '/dashboard/billing?payment=success',
        )

        try:
            confirmation_url = BillingService.create_payment(
                tenant=request.tenant,
                plan_slug=plan_slug,
                period=period,
                return_url=return_url,
                idempotency_key=serializer.validated_data['idempotency_key'],
            )
        except (
            CheckoutConflictError,
            CheckoutKeyLimitError,
            CheckoutManualReviewError,
            CheckoutPendingError,
            CheckoutTerminalError,
        ) as exc:
            return _checkout_error_response(exc)
        return Response({'status': 'ok', 'data': {'payment_url': confirmation_url}})


@method_decorator(transaction.non_atomic_requests, name='dispatch')
@extend_schema(tags=['Billing'], exclude=True)
class YooKassaWebhookView(APIView):
    """
    POST /api/v1/billing/webhook/yookassa/ — вебхук от YooKassa.

    Проверяет IP отправителя и обрабатывает события платежей.
    """

    permission_classes = []   # публичный, аутентифицируется по IP
    authentication_classes = []

    def post(self, request):
        request_data = request.data if isinstance(request.data, dict) else {}
        event = _safe_webhook_text(request_data.get('event'), 80)
        raw_payment_obj = request_data.get('object')
        payment_obj = raw_payment_obj if isinstance(raw_payment_obj, dict) else {}
        raw_object_id = payment_obj.get('id')
        object_id = _safe_webhook_text(raw_object_id, 200)
        untrusted_amount, untrusted_currency = _parse_webhook_amount(payment_obj)
        source_ip = _webhook_source_ip(request)
        safe_payload = _sanitize_webhook_payload(event, payment_obj)

        if not is_yookassa_ip(request):
            BillingWebhookEvent.objects.create(
                event_type=event,
                object_id=object_id,
                amount=untrusted_amount,
                currency=untrusted_currency,
                decision=BillingWebhookEvent.DECISION_REJECTED,
                reason='IP-адрес отправителя не принадлежит YooKassa.',
                payload=safe_payload,
                source_ip=source_ip,
                processed_at=timezone.now(),
            )
            return Response(
                {'status': 'error', 'code': 'forbidden', 'message': 'IP не авторизован'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        webhook_event, processing_token, claim_state = _claim_webhook_event(
            event=event,
            object_id=object_id,
            safe_payload=safe_payload,
            source_ip=source_ip,
        )
        if claim_state == 'final':
            return Response({'status': 'ok'})
        if claim_state == 'busy':
            return _retry_webhook_response('already_processing')
        result = process_claimed_yookassa_event(
            webhook_event.pk,
            processing_token,
        )
        if result.acknowledged:
            return Response({'status': 'ok'})
        return _retry_webhook_response(result.retry_code)
