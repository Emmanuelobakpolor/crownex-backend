from django.urls import path

from . import views

urlpatterns = [
    path('conversation/', views.ConversationView.as_view(), name='support-conversation'),
    path('messages/', views.SendMessageView.as_view(), name='support-send'),
    path('typing/', views.TypingView.as_view(), name='support-typing'),
    path('unread/', views.UnreadCountView.as_view(), name='support-unread'),
]
