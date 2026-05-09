from django.urls import path

from apps.billing.views import (
    CheckoutView,
    InvoiceListView,
    PlanListView,
    SubscriptionView,
    UsageView,
    YooKassaWebhookView,
)

urlpatterns = [
    path('billing/plans/', PlanListView.as_view(), name='billing-plans'),
    path('billing/subscription/', SubscriptionView.as_view(), name='billing-subscription'),
    path('billing/usage/', UsageView.as_view(), name='billing-usage'),
    path('billing/invoices/', InvoiceListView.as_view(), name='billing-invoices'),
    path('billing/checkout/', CheckoutView.as_view(), name='billing-checkout'),
    path('billing/webhook/yookassa/', YooKassaWebhookView.as_view(), name='billing-webhook-yookassa'),
]
