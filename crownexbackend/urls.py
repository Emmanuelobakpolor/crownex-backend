"""
URL configuration for crownexbackend project.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from accounts.views import ProfileView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/auth/', include('accounts.urls')),
    path('api/profile/', ProfileView.as_view(), name='profile'),
    path('api/wallet/', include('wallet.urls')),
    path('api/vtu/', include('vtu.urls')),
    path('api/giftcards/', include('giftcards.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
