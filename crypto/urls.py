from django.urls import path

from . import views

urlpatterns = [
    path('prices/', views.PricesView.as_view(), name='crypto-prices'),
    path('fees/', views.FeesView.as_view(), name='crypto-fees'),
    path('wallets/', views.WalletsView.as_view(), name='crypto-wallets'),
    path('quote/', views.QuoteView.as_view(), name='crypto-quote'),
    path('orders/', views.OrderHistoryView.as_view(), name='crypto-orders'),
    path('orders/buy/', views.BuyOrderView.as_view(), name='crypto-buy'),
    path('orders/buy/verify/', views.VerifyBuyPaymentView.as_view(), name='crypto-buy-verify'),
    path(
        'orders/<str:reference>/proof/',
        views.PaymentProofUploadView.as_view(),
        name='crypto-order-proof',
    ),
    path('orders/sell/', views.SellOrderView.as_view(), name='crypto-sell'),
    path(
        'orders/sell/<str:reference>/retry/',
        views.RetrySellOrderView.as_view(),
        name='crypto-sell-retry',
    ),
    path('orders/swap/', views.SwapOrderView.as_view(), name='crypto-swap'),
    path('address/<str:coin>/', views.DepositAddressView.as_view(), name='crypto-address'),
    path('webhook/quidax/', views.QuidaxWebhookView.as_view(), name='crypto-webhook'),
    path('withdraw/', views.WithdrawView.as_view(), name='crypto-withdraw'),
    path(
        'withdraw/estimate/',
        views.WithdrawEstimateView.as_view(),
        name='crypto-withdraw-estimate',
    ),
    path('withdrawals/', views.WithdrawalHistoryView.as_view(), name='crypto-withdrawals'),
]
