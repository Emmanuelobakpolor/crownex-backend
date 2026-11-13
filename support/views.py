"""Support chat API — the user's side and the admin inbox.

User endpoints live under /api/support/, admin ones under
/api/admin/support/ (see support/admin_urls.py). Both sides poll every
couple of seconds while a chat is open; `?after=<message id>` keeps each
poll down to just the new messages.
"""

from django.core.paginator import Paginator
from django.db.models import Count, F, OuterRef, Q, Subquery
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import ConversationStatus, SenderType, SupportConversation, SupportMessage
from .serializers import (
    AdminConversationSerializer,
    ConversationStatusSerializer,
    SendMessageSerializer,
    SupportMessageSerializer,
    TypingSerializer,
)

# First load of a thread returns this many of the latest messages.
HISTORY_LIMIT = 200


def _messages_payload(conversation: SupportConversation, after_param) -> list:
    qs = conversation.messages.select_related('admin', 'conversation__user')
    try:
        after = int(after_param) if after_param else None
    except (TypeError, ValueError):
        after = None
    if after is not None:
        qs = qs.filter(id__gt=after)
    else:
        # Newest HISTORY_LIMIT, returned oldest-first.
        qs = qs.order_by('-id')[:HISTORY_LIMIT]
        return SupportMessageSerializer(reversed(list(qs)), many=True).data
    return SupportMessageSerializer(qs, many=True).data


def _post_message(conversation, sender_type, body, admin=None) -> SupportMessage:
    message = SupportMessage.objects.create(
        conversation=conversation, sender_type=sender_type, body=body, admin=admin
    )
    now = message.created_at
    updates = {'last_message_at': now, 'updated_at': timezone.now()}
    if sender_type == SenderType.USER:
        # A new message from the user reopens a resolved chat; sending
        # also means they've stopped typing and have read everything.
        updates.update(status=ConversationStatus.OPEN, user_typing_at=None, user_last_read_at=now)
    else:
        updates.update(admin_typing_at=None, admin_last_read_at=now)
    SupportConversation.objects.filter(pk=conversation.pk).update(**updates)
    return message


# ─── User side ───────────────────────────────────────────────────────────────


class ConversationView(APIView):
    """GET /api/support/conversation/?after=<id>

    Returns the user's thread (creating it on first open), any messages
    newer than `after`, and whether an agent is typing. Fetching marks the
    agent's messages as read — the app only polls this while the chat
    screen is open.
    """

    def get(self, request):
        conversation, _ = SupportConversation.objects.get_or_create(user=request.user)
        messages = _messages_payload(conversation, request.query_params.get('after'))

        now = timezone.now()
        SupportConversation.objects.filter(pk=conversation.pk).update(user_last_read_at=now)

        return Response(
            {
                'id': conversation.id,
                'status': conversation.status,
                'messages': messages,
                'admin_typing': conversation.admin_is_typing,
                'admin_last_read_at': conversation.admin_last_read_at,
            }
        )


class SendMessageView(APIView):
    """POST /api/support/messages/ — { body }"""

    def post(self, request):
        serializer = SendMessageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        conversation, _ = SupportConversation.objects.get_or_create(user=request.user)
        message = _post_message(conversation, SenderType.USER, serializer.validated_data['body'])
        return Response(SupportMessageSerializer(message).data, status=status.HTTP_201_CREATED)


class TypingView(APIView):
    """POST /api/support/typing/ — { typing: bool }"""

    def post(self, request):
        serializer = TypingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        typing_at = timezone.now() if serializer.validated_data['typing'] else None
        SupportConversation.objects.filter(user=request.user).update(user_typing_at=typing_at)
        return Response(status=status.HTTP_204_NO_CONTENT)


class UnreadCountView(APIView):
    """GET /api/support/unread/ — agent replies the user hasn't seen yet.

    Read-only (doesn't mark anything read), for badges outside the chat.
    """

    def get(self, request):
        conversation = SupportConversation.objects.filter(user=request.user).first()
        if conversation is None:
            return Response({'unread': 0})
        qs = conversation.messages.filter(sender_type=SenderType.ADMIN)
        if conversation.user_last_read_at:
            qs = qs.filter(created_at__gt=conversation.user_last_read_at)
        return Response({'unread': qs.count()})


# ─── Admin side ──────────────────────────────────────────────────────────────


def _get_conversation(pk):
    return SupportConversation.objects.select_related('user').filter(pk=pk).first()


def _not_found():
    return Response({'detail': 'Conversation not found.'}, status=status.HTTP_404_NOT_FOUND)


class AdminConversationListView(APIView):
    """GET /api/admin/support/conversations/?status=&search=&page=&page_size=

    Only threads with at least one message, most recent activity first.
    `unread_total` counts unread user messages across every open thread,
    for the sidebar badge.
    """

    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        latest = SupportMessage.objects.filter(conversation=OuterRef('pk')).order_by('-id')
        unread_filter = Q(messages__sender_type=SenderType.USER) & (
            Q(admin_last_read_at__isnull=True)
            | Q(messages__created_at__gt=F('admin_last_read_at'))
        )
        qs = (
            SupportConversation.objects.filter(last_message_at__isnull=False)
            .select_related('user')
            .annotate(
                last_body=Subquery(latest.values('body')[:1]),
                last_sender_type=Subquery(latest.values('sender_type')[:1]),
                admin_unread=Count('messages', filter=unread_filter),
            )
            .order_by('-last_message_at')
        )

        unread_total = sum(qs.values_list('admin_unread', flat=True))

        status_filter = request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        search = request.query_params.get('search', '').strip()
        if search:
            qs = qs.filter(
                Q(user__email__icontains=search)
                | Q(user__full_name__icontains=search)
                | Q(user__phone__icontains=search)
            )

        page_size = min(int(request.query_params.get('page_size', 50) or 50), 100)
        paginator = Paginator(qs, page_size)
        page_obj = paginator.get_page(request.query_params.get('page', 1))

        return Response(
            {
                'count': paginator.count,
                'page': page_obj.number,
                'page_size': page_size,
                'num_pages': paginator.num_pages,
                'unread_total': unread_total,
                'results': AdminConversationSerializer(
                    page_obj.object_list, many=True, context={'request': request}
                ).data,
            }
        )


class AdminConversationDetailView(APIView):
    """GET /api/admin/support/conversations/<id>/?after=<id>

    Messages (all recent, or just newer than `after`), whether the user is
    typing, and when they last read the thread. Marks the thread read for
    the admin team.
    """

    permission_classes = [permissions.IsAdminUser]

    def get(self, request, pk):
        conversation = _get_conversation(pk)
        if conversation is None:
            return _not_found()
        messages = _messages_payload(conversation, request.query_params.get('after'))

        conversation.admin_last_read_at = timezone.now()
        SupportConversation.objects.filter(pk=pk).update(
            admin_last_read_at=conversation.admin_last_read_at
        )

        return Response(
            {
                'conversation': AdminConversationSerializer(
                    conversation, context={'request': request}
                ).data,
                'messages': messages,
            }
        )


class AdminSendMessageView(APIView):
    """POST /api/admin/support/conversations/<id>/messages/ — { body }"""

    permission_classes = [permissions.IsAdminUser]

    def post(self, request, pk):
        conversation = _get_conversation(pk)
        if conversation is None:
            return _not_found()
        serializer = SendMessageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        message = _post_message(
            conversation,
            SenderType.ADMIN,
            serializer.validated_data['body'],
            admin=request.user,
        )
        return Response(SupportMessageSerializer(message).data, status=status.HTTP_201_CREATED)


class AdminTypingView(APIView):
    """POST /api/admin/support/conversations/<id>/typing/ — { typing: bool }"""

    permission_classes = [permissions.IsAdminUser]

    def post(self, request, pk):
        serializer = TypingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        typing_at = timezone.now() if serializer.validated_data['typing'] else None
        updated = SupportConversation.objects.filter(pk=pk).update(admin_typing_at=typing_at)
        if not updated:
            return _not_found()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminConversationStatusView(APIView):
    """POST /api/admin/support/conversations/<id>/status/ — { status: open|resolved }"""

    permission_classes = [permissions.IsAdminUser]

    def post(self, request, pk):
        serializer = ConversationStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        updated = SupportConversation.objects.filter(pk=pk).update(
            status=serializer.validated_data['status']
        )
        if not updated:
            return _not_found()
        return Response({'status': serializer.validated_data['status']})
