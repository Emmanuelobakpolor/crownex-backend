"""API views for the CrownEx admin panel.

Two entry points are open (create + login); everything else requires an
authenticated staff account (IsAdminUser -> request.user.is_staff).
"""

from django.conf import settings
from django.contrib.auth import authenticate
from django.core.paginator import Paginator
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from accounts.services import issue_tokens

from . import services
from .serializers import (
    AdminLoginSerializer,
    AdminProfileUpdateSerializer,
    CreateAdminSerializer,
)


class CreateAdminView(APIView):
    """POST /api/admin/create/ — bootstrap an admin account from Postman.

    There's no admin session to gate this behind when the very first admin
    is being created, so it's gated by a shared secret instead: set
    ADMIN_REGISTRATION_SECRET in the backend's environment, then send the
    same value as the X-Admin-Secret header.
    """

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        configured_secret = settings.ADMIN_REGISTRATION_SECRET
        if not configured_secret:
            return Response(
                {'detail': 'Admin registration is not configured on this server.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if request.headers.get('X-Admin-Secret') != configured_secret:
            return Response(
                {'detail': 'Invalid admin secret.'}, status=status.HTTP_403_FORBIDDEN
            )

        serializer = CreateAdminSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        user = User.objects.create_superuser(
            email=data['email'],
            password=data['password'],
            full_name=data.get('full_name', ''),
        )

        return Response(
            {
                'message': 'Admin account created. Log in at POST /api/admin/login/.',
                'email': user.email,
                'full_name': user.full_name,
            },
            status=status.HTTP_201_CREATED,
        )


class AdminLoginView(APIView):
    """POST /api/admin/login/ — email + password -> JWT, staff accounts only."""

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = AdminLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data['email']
        password = serializer.validated_data['password']

        try:
            user = User.objects.get(email__iexact=email)
        except User.DoesNotExist:
            return Response(
                {'detail': 'Invalid email or password.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if not user.is_staff:
            return Response(
                {'detail': 'This account does not have admin access.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not user.is_active:
            return Response(
                {'detail': 'This account has been deactivated.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        authenticated = authenticate(username=email, password=password)
        if authenticated is None and not user.check_password(password):
            return Response(
                {'detail': 'Invalid email or password.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        tokens = issue_tokens(user)
        return Response(
            {
                'message': 'Login successful.',
                'admin': {
                    'id': user.id,
                    'email': user.email,
                    'full_name': user.full_name,
                },
                'access': tokens['access'],
                'refresh': tokens['refresh'],
            }
        )


class AdminOverviewView(APIView):
    """GET /api/admin/overview/ — dashboard stat cards."""

    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        return Response(services.get_overview_stats())


class AdminUserListView(APIView):
    """GET /api/admin/users/?search=&page=&page_size= — paginated user list."""

    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        search = request.query_params.get('search', '').strip()
        page_size = min(int(request.query_params.get('page_size', 25) or 25), 100)
        page = request.query_params.get('page', 1)

        paginator = Paginator(services.get_users(search), page_size)
        page_obj = paginator.get_page(page)

        return Response(
            {
                'count': paginator.count,
                'page': page_obj.number,
                'page_size': page_size,
                'num_pages': paginator.num_pages,
                'results': [services.serialize_user(u) for u in page_obj.object_list],
            }
        )


class AdminTransactionListView(APIView):
    """GET /api/admin/transactions/?type=&page=&page_size= — unified feed.

    type is one of: all, deposit, withdrawal, airtime, data, cable,
    electricity, vtu (all VTU services), giftcard.
    """

    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        tx_type = request.query_params.get('type', 'all')
        page = int(request.query_params.get('page', 1) or 1)
        page_size = min(int(request.query_params.get('page_size', 25) or 25), 100)

        items = services.get_unified_transactions(tx_type)
        start = (page - 1) * page_size
        end = start + page_size

        return Response(
            {
                'count': len(items),
                'page': page,
                'page_size': page_size,
                'results': items[start:end],
            }
        )


class AdminProfileView(APIView):
    """GET/PATCH /api/admin/profile/ — the logged-in admin's own account."""

    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        return Response(
            {
                'id': request.user.id,
                'email': request.user.email,
                'full_name': request.user.full_name,
            }
        )

    def patch(self, request):
        serializer = AdminProfileUpdateSerializer(
            data=request.data, context={'user': request.user}
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        user = request.user
        if data.get('new_email'):
            user.email = data['new_email']
        if 'full_name' in data:
            user.full_name = data['full_name']
        user.save()

        return Response(
            {
                'message': 'Profile updated successfully.',
                'id': user.id,
                'email': user.email,
                'full_name': user.full_name,
            }
        )
