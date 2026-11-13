from rest_framework import serializers

from .models import SenderType, SupportConversation, SupportMessage

MAX_MESSAGE_LENGTH = 2000


class SupportMessageSerializer(serializers.ModelSerializer):
    sender_name = serializers.SerializerMethodField()

    class Meta:
        model = SupportMessage
        fields = ['id', 'sender_type', 'sender_name', 'body', 'created_at']

    def get_sender_name(self, obj: SupportMessage) -> str:
        if obj.sender_type == SenderType.USER:
            return obj.conversation.user.full_name or obj.conversation.user.email
        # Users see the agent's first name ("Ada"), never their email.
        if obj.admin and obj.admin.full_name:
            return obj.admin.full_name.split()[0]
        return 'Crownex Support'


class SendMessageSerializer(serializers.Serializer):
    body = serializers.CharField(max_length=MAX_MESSAGE_LENGTH, trim_whitespace=True)


class TypingSerializer(serializers.Serializer):
    typing = serializers.BooleanField(default=True)


class ConversationStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=['open', 'resolved'])


def _avatar_url(user, request):
    if not user.profile_picture:
        return None
    url = user.profile_picture.url
    return request.build_absolute_uri(url) if request else url


class AdminConversationSerializer(serializers.ModelSerializer):
    """Inbox row — who, last message preview, unread count, typing."""

    user = serializers.SerializerMethodField()
    last_message = serializers.SerializerMethodField()
    unread_count = serializers.SerializerMethodField()
    user_is_typing = serializers.BooleanField(read_only=True)

    class Meta:
        model = SupportConversation
        fields = [
            'id',
            'user',
            'status',
            'last_message',
            'last_message_at',
            'unread_count',
            'user_is_typing',
            'user_last_read_at',
            'created_at',
        ]

    def get_user(self, obj: SupportConversation):
        user = obj.user
        return {
            'id': user.id,
            'email': user.email,
            'full_name': user.full_name,
            'phone': user.phone or '',
            'profile_picture': _avatar_url(user, self.context.get('request')),
        }

    def get_last_message(self, obj: SupportConversation):
        # The list view annotates these to avoid a query per row.
        if hasattr(obj, 'last_body'):
            if obj.last_body is None:
                return None
            return {'body': obj.last_body, 'sender_type': obj.last_sender_type}
        latest = obj.messages.order_by('-id').first()
        if latest is None:
            return None
        return {'body': latest.body, 'sender_type': latest.sender_type}

    def get_unread_count(self, obj: SupportConversation) -> int:
        annotated = getattr(obj, 'admin_unread', None)
        if annotated is not None:
            return annotated
        qs = obj.messages.filter(sender_type=SenderType.USER)
        if obj.admin_last_read_at:
            qs = qs.filter(created_at__gt=obj.admin_last_read_at)
        return qs.count()
