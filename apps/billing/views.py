from django.conf import settings
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.models import Invoice, Plan
from apps.billing.serializers import CheckoutSerializer, InvoiceSerializer, PlanSerializer, SubscriptionSerializer
from apps.billing.services import BillingService, LimitChecker
from apps.billing.webhook import is_yookassa_ip


class PlanListView(APIView):
    """GET /api/v1/billing/plans/ — список доступных тарифов."""

    permission_classes = []   # публичный эндпоинт

    def get(self, request):
        plans = Plan.objects.filter(is_active=True)
        return Response({
            'status': 'ok',
            'data': PlanSerializer(plans, many=True).data,
        })


class SubscriptionView(APIView):
    """GET /api/v1/billing/subscription/ — текущая подписка тенанта."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            sub = request.tenant.subscription
        except Exception:
            return Response({'status': 'ok', 'data': None})

        return Response({
            'status': 'ok',
            'data': SubscriptionSerializer(sub).data,
        })


class UsageView(APIView):
    """GET /api/v1/billing/usage/ — использование лимитов тенантом."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        summary = LimitChecker().get_usage_summary(request.tenant)
        return Response({'status': 'ok', 'data': summary})


class InvoiceListView(APIView):
    """GET /api/v1/billing/invoices/ — история платежей тенанта."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        invoices = Invoice.objects.filter(tenant=request.tenant).order_by('-created_at')
        return Response({
            'status': 'ok',
            'data': InvoiceSerializer(invoices, many=True).data,
        })


class CheckoutView(APIView):
    """
    POST /api/v1/billing/checkout/ — создать платёж YooKassa.

    Возвращает payment_url для редиректа пользователя на страницу оплаты.
    """

    permission_classes = [IsAuthenticated]

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


class YooKassaWebhookView(APIView):
    """
    POST /api/v1/billing/webhook/yookassa/ — вебхук от YooKassa.

    Проверяет IP отправителя и обрабатывает события платежей.
    """

    permission_classes = []   # публичный, аутентифицируется по IP
    authentication_classes = []

    def post(self, request):
        if not is_yookassa_ip(request):
            return Response(
                {'status': 'error', 'code': 'forbidden', 'message': 'IP не авторизован'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        event = request.data.get('event', '')
        payment_obj = request.data.get('object', {})
        payment_id = payment_obj.get('id', '')
        amount = payment_obj.get('amount', {}).get('value', '0')
        metadata = payment_obj.get('metadata', {})

        if event == 'payment.succeeded':
            BillingService.handle_payment_success_webhook(payment_id, amount, metadata)
        elif event == 'payment.canceled':
            BillingService.handle_payment_failed_webhook(payment_id)

        return Response({'status': 'ok'})
