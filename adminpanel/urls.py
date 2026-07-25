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
]
