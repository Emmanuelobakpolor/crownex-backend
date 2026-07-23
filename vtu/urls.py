from django.urls import path

from . import views

urlpatterns = [
    path('catalogue/', views.CatalogueView.as_view(), name='vtu-catalogue'),
    path('airtime/', views.BuyAirtimeView.as_view(), name='vtu-buy-airtime'),
    path('data/', views.BuyDataView.as_view(), name='vtu-buy-data'),
    path(
        'cable/bouquets/',
        views.CableBouquetsView.as_view(),
        name='vtu-cable-bouquets',
    ),
    path(
        'cable/verify/',
        views.VerifySmartcardView.as_view(),
        name='vtu-cable-verify',
    ),
    path('cable/', views.BuyCableView.as_view(), name='vtu-buy-cable'),
    path(
        'electricity/variations/',
        views.ElectricityVariationsView.as_view(),
        name='vtu-electricity-variations',
    ),
    path(
        'electricity/verify/',
        views.VerifyMeterView.as_view(),
        name='vtu-electricity-verify',
    ),
    path(
        'electricity/',
        views.BuyElectricityView.as_view(),
        name='vtu-buy-electricity',
    ),
    path(
        'transactions/',
        views.TransactionHistoryView.as_view(),
        name='vtu-transactions',
    ),
    path(
        'transactions/<str:reference>/status/',
        views.TransactionStatusView.as_view(),
        name='vtu-transaction-status',
    ),
    path(
        'webhook/<str:secret>/',
        views.PluginNGWebhookView.as_view(),
        name='vtu-webhook',
    ),
]
