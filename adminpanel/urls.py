from django.urls import path

from . import views

urlpatterns = [
    path('create/', views.CreateAdminView.as_view(), name='admin-create'),
    path('login/', views.AdminLoginView.as_view(), name='admin-login'),
    path('overview/', views.AdminOverviewView.as_view(), name='admin-overview'),
    path('users/', views.AdminUserListView.as_view(), name='admin-users'),
    path(
        'transactions/',
        views.AdminTransactionListView.as_view(),
        name='admin-transactions',
    ),
    path('profile/', views.AdminProfileView.as_view(), name='admin-profile'),
    path('fees/', views.AdminCryptoFeeListView.as_view(), name='admin-crypto-fees'),
    path(
        'fees/<int:pk>/',
        views.AdminCryptoFeeUpdateView.as_view(),
        name='admin-crypto-fee-update',
    ),
    path(
        'crypto/orders/',
        views.AdminCryptoOrderListView.as_view(),
        name='admin-crypto-orders',
    ),
    path(
        'crypto/orders/<str:reference>/',
        views.AdminCryptoOrderActionView.as_view(),
        name='admin-crypto-order-action',
    ),
    path(
        'crypto/withdrawals/',
        views.AdminCryptoWithdrawalListView.as_view(),
        name='admin-crypto-withdrawals',
    ),
    path(
        'crypto/withdrawals/<str:reference>/',
        views.AdminCryptoWithdrawalActionView.as_view(),
        name='admin-crypto-withdrawal-action',
    ),
]
