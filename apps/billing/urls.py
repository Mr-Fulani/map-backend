from django.urls import path

from apps.billing.views import InvoiceListView, PlanListView, SubscriptionView, UsageView

urlpatterns = [
    path('billing/plans/', PlanListView.as_view(), name='billing-plans'),
    path('billing/subscription/', SubscriptionView.as_view(), name='billing-subscription'),
    path('billing/usage/', UsageView.as_view(), name='billing-usage'),
    path('billing/invoices/', InvoiceListView.as_view(), name='billing-invoices'),
]
