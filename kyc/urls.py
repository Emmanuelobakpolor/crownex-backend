from django.urls import path

from . import views

urlpatterns = [
    path('submit/', views.SubmitVerificationView.as_view(), name='kyc-submit'),
    path('status/', views.VerificationStatusView.as_view(), name='kyc-status'),
]
