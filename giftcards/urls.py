from django.urls import path

from . import views

urlpatterns = [
    path('rate/', views.RateView.as_view(), name='giftcards-rate'),
    path('brands/', views.BrandsView.as_view(), name='giftcards-brands'),
    path('products/', views.ProductsView.as_view(), name='giftcards-products'),
    path('buy/', views.BuyView.as_view(), name='giftcards-buy'),
    path('history/', views.HistoryView.as_view(), name='giftcards-history'),
    path('webhook/<str:secret>/', views.WebhookView.as_view(), name='giftcards-webhook'),
]
