"""In-app support chat between a user and the admin team.

One SupportConversation per user (OneToOneField, same pattern as
KycVerification) — the user always lands back in the same thread, and any
admin can pick it up from the admin panel's Support inbox.

There's no websocket layer (the API runs on plain gunicorn), so both sides
poll: "typing…" is a timestamp that counts as live for TYPING_TTL, and read
receipts are each side's last_read_at compared against message times.
"""

from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

# How long a typing ping stays "live". Clients re-ping every few seconds
# while the person keeps typing, so this only needs to outlast one ping
# plus one poll.
TYPING_TTL = timedelta(seconds=6)


class ConversationStatus(models.TextChoices):
    OPEN = 'open', 'Open'
    RESOLVED = 'resolved', 'Resolved'


class SenderType(models.TextChoices):
    USER = 'user', 'User'
    ADMIN = 'admin', 'Admin'


class SupportConversation(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='support_conversation',
    )
    status = models.CharField(
        max_length=10, choices=ConversationStatus.choices, default=ConversationStatus.OPEN
    )

    last_message_at = models.DateTimeField(null=True, blank=True, db_index=True)

    user_typing_at = models.DateTimeField(null=True, blank=True)
    admin_typing_at = models.DateTimeField(null=True, blank=True)

    user_last_read_at = models.DateTimeField(null=True, blank=True)
    admin_last_read_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-last_message_at', '-created_at']

    def __str__(self):
        return f'Support chat — {self.user.email}'

    @staticmethod
    def _is_live(ts) -> bool:
        return ts is not None and timezone.now() - ts < TYPING_TTL

    @property
    def user_is_typing(self) -> bool:
        return self._is_live(self.user_typing_at)

    @property
    def admin_is_typing(self) -> bool:
        return self._is_live(self.admin_typing_at)


class SupportMessage(models.Model):
    conversation = models.ForeignKey(
        SupportConversation, on_delete=models.CASCADE, related_name='messages'
    )
    sender_type = models.CharField(max_length=10, choices=SenderType.choices)
    # Which admin replied — null for the user's own messages, and kept
    # (SET_NULL) if that admin account is later removed.
    admin = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f'{self.sender_type}: {self.body[:40]}'
