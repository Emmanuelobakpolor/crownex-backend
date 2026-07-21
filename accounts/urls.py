from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from . import views

urlpatterns = [
    # Registration pipeline (matches Flutter multi-step flow)
    path('register/', views.RegisterView.as_view(), name='auth-register'),
    path('verify-otp/', views.VerifyOTPView.as_view(), name='auth-verify-otp'),
    path('verify-email/', views.VerifyEmailView.as_view(), name='auth-verify-email'),
    path('resend-otp/', views.ResendOTPView.as_view(), name='auth-resend-otp'),
    path(
        'resend-verification/',
        views.ResendVerificationView.as_view(),
        name='auth-resend-verification',
    ),
    path(
        'complete-profile/',
        views.CompleteProfileView.as_view(),
        name='auth-complete-profile',
    ),
    path(
        'set-transaction-pin/',
        views.SetTransactionPinView.as_view(),
        name='auth-set-transaction-pin',
    ),
    # Session
    path('login/', views.LoginView.as_view(), name='auth-login'),
    path('logout/', views.LogoutView.as_view(), name='auth-logout'),
    path('refresh/', TokenRefreshView.as_view(), name='auth-refresh'),
    # Authenticated account changes
    path(
        'change-password/',
        views.ChangePasswordView.as_view(),
        name='auth-change-password',
    ),
    path(
        'change-transaction-pin/',
        views.ChangeTransactionPinView.as_view(),
        name='auth-change-transaction-pin',
    ),
    # Password reset
    path(
        'forgot-password/',
        views.ForgotPasswordView.as_view(),
        name='auth-forgot-password',
    ),
    path(
        'verify-reset-token/',
        views.VerifyResetTokenView.as_view(),
        name='auth-verify-reset-token',
    ),
    path(
        'reset-password/',
        views.ResetPasswordView.as_view(),
        name='auth-reset-password',
    ),
]
