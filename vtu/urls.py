from django.urls import path

from . import views

urlpatterns = [
    path('catalogue/', views.CatalogueView.as_view(), name='vtu-catalogue'),
    path('airtime/', views.BuyAirtimeView.as_view(), name='vtu-buy-airtime'),
    path('data/', views.BuyDataView.as_view(), name='vtu-buy-data'),
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
