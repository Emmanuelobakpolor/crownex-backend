from django.urls import path

from . import views

urlpatterns = [
    path('request/', views.RequestCardView.as_view(), name='cards-request'),
    path('status/', views.CardStatusView.as_view(), name='cards-status'),
    path('fund/', views.FundCardView.as_view(), name='cards-fund'),
    path('withdraw/', views.WithdrawCardView.as_view(), name='cards-withdraw'),
    path('freeze/', views.FreezeCardView.as_view(), name='cards-freeze'),
    path('unfreeze/', views.UnfreezeCardView.as_view(), name='cards-unfreeze'),
    path('terminate/', views.TerminateCardView.as_view(), name='cards-terminate'),
    path('transactions/', views.CardTransactionHistoryView.as_view(), name='cards-transactions'),
]
