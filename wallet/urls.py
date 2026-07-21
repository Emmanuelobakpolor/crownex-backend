from django.urls import path

from . import views

urlpatterns = [
    path('balance/', views.WalletBalanceView.as_view(), name='wallet-balance'),
    path(
        'initiate-deposit/',
        views.InitiateDepositView.as_view(),
        name='wallet-initiate-deposit',
    ),
    path(
        'verify-payment/',
        views.VerifyPaymentView.as_view(),
        name='wallet-verify-payment',
    ),
    path(
        'flw-webhook/',
        views.FlutterwaveWebhookView.as_view(),
        name='wallet-flw-webhook',
    ),
    path('banks/', views.BanksView.as_view(), name='wallet-banks'),
    path(
        'resolve-account/',
        views.ResolveAccountView.as_view(),
        name='wallet-resolve-account',
    ),
    path('withdraw/', views.WithdrawView.as_view(), name='wallet-withdraw'),
    path(
        'transactions/',
        views.TransactionHistoryView.as_view(),
        name='wallet-transactions',
    ),
]
