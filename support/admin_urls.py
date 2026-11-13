from django.urls import path

from . import views

urlpatterns = [
    path(
        'conversations/',
        views.AdminConversationListView.as_view(),
        name='admin-support-conversations',
    ),
    path(
        'conversations/<int:pk>/',
        views.AdminConversationDetailView.as_view(),
        name='admin-support-conversation',
    ),
    path(
        'conversations/<int:pk>/messages/',
        views.AdminSendMessageView.as_view(),
        name='admin-support-send',
    ),
    path(
        'conversations/<int:pk>/typing/',
        views.AdminTypingView.as_view(),
        name='admin-support-typing',
    ),
    path(
        'conversations/<int:pk>/status/',
        views.AdminConversationStatusView.as_view(),
        name='admin-support-status',
    ),
]
