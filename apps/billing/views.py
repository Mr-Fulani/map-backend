import ipaddress
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db.models import F
from django.utils import timezone
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.models import (
    AICreditPackage, BillingWebhookEvent, Invoice, PaymentReversal, Plan,
)
from apps.billing.serializers import (
    AICreditPackageSerializer, AITopupCheckoutSerializer, CheckoutSerializer,
    InvoiceSerializer, PlanSerializer, SubscriptionSerializer,
)
from apps.billing.services import BillingService, LimitChecker
from apps.billing.webhook import is_yookassa_ip
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
            'id', 'status', 'payment_id', 'amount', 'metadata',
            'created_at', 'cancellation_details',
        )
        if key in obj
    }
    return {'type': 'notification', 'event': event, 'object': allowed}


def _parse_webhook_amount(obj: dict) -> tuple[Decimal | None, str]:
    amount_obj = obj.get('amount') or {}
    try:
        amount = Decimal(str(amount_obj.get('value'))).quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError, ValueError):
        amount = None
    return amount, str(amount_obj.get('currency') or '').upper()


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


@extend_schema(tags=['Billing'])
class AITopupCheckoutView(APIView):
    permission_classes = [IsAuthenticated, TenantOwnerPermission]

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
            f'{settings.SITE_URL}/dashboard/billing?topup=success',
        )
        try:
            confirmation_url = BillingService.create_ai_topup_payment(
                tenant=request.tenant,
                package_id=serializer.validated_data['package_id'],
                return_url=return_url,
            )
        except AICreditPackage.DoesNotExist:
            return Response(
                {'status': 'error', 'code': 'package_not_found'},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response({'status': 'ok', 'data': {'payment_url': confirmation_url}})


@extend_schema(tags=['Billing'])
class CheckoutView(APIView):
    """
    POST /api/v1/billing/checkout/ — создать платёж YooKassa.

    Возвращает payment_url для редиректа пользователя на страницу оплаты.
    """

    permission_classes = [IsAuthenticated, TenantOwnerPermission]

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
            f'{settings.SITE_URL}/billing/success/',
        )

        confirmation_url = BillingService.create_payment(
            tenant=request.tenant,
            plan_slug=plan_slug,
            period=period,
            return_url=return_url,
        )
        return Response({'status': 'ok', 'data': {'payment_url': confirmation_url}})


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
        event = str(request_data.get('event') or '')
        raw_payment_obj = request_data.get('object')
        payment_obj = raw_payment_obj if isinstance(raw_payment_obj, dict) else {}
        object_id = str(payment_obj.get('id') or '')
        payment_id = (
            str(payment_obj.get('payment_id') or '')
            if event.startswith('refund.')
            else object_id
        )
        amount, currency = _parse_webhook_amount(payment_obj)
        source_ip = _webhook_source_ip(request)
        safe_payload = _sanitize_webhook_payload(event, payment_obj)

        if not is_yookassa_ip(request):
            BillingWebhookEvent.objects.create(
                event_type=event,
                object_id=object_id,
                payment_id=payment_id,
                amount=amount,
                currency=currency,
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

        idempotency_key = f'{event}:{object_id}' if event and object_id else ''
        webhook_event = None
        created = True
        if idempotency_key:
            webhook_event, created = BillingWebhookEvent.objects.get_or_create(
                provider='yookassa',
                idempotency_key=idempotency_key,
                defaults={
                    'event_type': event,
                    'object_id': object_id,
                    'payment_id': payment_id,
                    'amount': amount,
                    'currency': currency,
                    'payload': safe_payload,
                    'source_ip': source_ip,
                },
            )
        else:
            webhook_event = BillingWebhookEvent.objects.create(
                event_type=event,
                object_id=object_id,
                payment_id=payment_id,
                amount=amount,
                currency=currency,
                payload=safe_payload,
                source_ip=source_ip,
            )

        if not created:
            BillingWebhookEvent.objects.filter(pk=webhook_event.pk).update(
                delivery_count=F('delivery_count') + 1,
                updated_at=timezone.now(),
            )
            if webhook_event.decision not in (
                BillingWebhookEvent.DECISION_RECEIVED,
                BillingWebhookEvent.DECISION_ERROR,
            ):
                return Response({'status': 'ok'})

        invoice = Invoice.objects.filter(
            yookassa_payment_id=payment_id,
        ).select_related('tenant').first()
        webhook_event.invoice = invoice
        webhook_event.tenant = invoice.tenant if invoice else None

        try:
            expected_status = event.partition('.')[2]
            object_status = str(payment_obj.get('status') or '')
            supported_events = {
                'payment.succeeded',
                'payment.canceled',
                'refund.succeeded',
            }
            if event in supported_events and (not object_id or not payment_id):
                webhook_event.decision = BillingWebhookEvent.DECISION_REJECTED
                webhook_event.reason = 'В webhook отсутствует обязательный идентификатор объекта.'
            elif event in supported_events and object_status != expected_status:
                webhook_event.decision = BillingWebhookEvent.DECISION_REJECTED
                webhook_event.reason = (
                    f'Статус объекта {object_status} не соответствует событию {event}.'
                )
            elif event == 'payment.succeeded':
                processed = BillingService.handle_payment_success_webhook(
                    payment_id,
                    amount,
                    payment_obj.get('metadata') or {},
                    currency=currency,
                )
                webhook_event.decision = (
                    BillingWebhookEvent.DECISION_APPLIED
                    if processed
                    else BillingWebhookEvent.DECISION_REJECTED
                )
                webhook_event.reason = (
                    '' if processed else 'Платёж не прошёл внутреннюю проверку.'
                )
            elif event == 'payment.canceled':
                was_pending = (
                    invoice is not None and invoice.status == Invoice.STATUS_PENDING
                )
                BillingService.handle_payment_failed_webhook(payment_id)
                webhook_event.decision = (
                    BillingWebhookEvent.DECISION_APPLIED
                    if was_pending
                    else BillingWebhookEvent.DECISION_IGNORED
                )
                webhook_event.reason = (
                    ''
                    if was_pending
                    else (
                        'Invoice для платежа не найден.'
                        if invoice is None
                        else f'Invoice уже находится в статусе {invoice.status}.'
                    )
                )
            elif event == 'refund.succeeded':
                reversal = BillingService.handle_reversal_success(
                    provider_reference=object_id,
                    payment_id=payment_id,
                    amount=amount,
                    currency=currency,
                )
                if reversal is None:
                    webhook_event.decision = BillingWebhookEvent.DECISION_REJECTED
                    webhook_event.reason = 'Возврат не прошёл внутреннюю проверку.'
                elif reversal.status == PaymentReversal.STATUS_APPLIED:
                    webhook_event.decision = BillingWebhookEvent.DECISION_APPLIED
                else:
                    webhook_event.decision = BillingWebhookEvent.DECISION_MANUAL_REVIEW
                    webhook_event.reason = reversal.reason
            else:
                webhook_event.decision = BillingWebhookEvent.DECISION_IGNORED
                webhook_event.reason = 'Неподдерживаемый тип события.'
        except Exception as exc:
            webhook_event.decision = BillingWebhookEvent.DECISION_ERROR
            webhook_event.reason = str(exc)[:500]
            webhook_event.processed_at = timezone.now()
            webhook_event.save(update_fields=[
                'invoice', 'tenant', 'decision', 'reason',
                'processed_at', 'updated_at',
            ])
            return Response(
                {'status': 'error', 'code': 'processing_error'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        webhook_event.processed_at = timezone.now()
        webhook_event.save(update_fields=[
            'invoice', 'tenant', 'decision', 'reason',
            'processed_at', 'updated_at',
        ])

        return Response({'status': 'ok'})
