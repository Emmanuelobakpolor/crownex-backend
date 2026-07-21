"""API views for CrownEx authentication and profile."""

from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenRefreshView

from . import services
from .serializers import (
    ChangePasswordSerializer,
    ChangeTransactionPinSerializer,
    CompleteProfileSerializer,
    ForgotPasswordSerializer,
    LoginSerializer,
    LogoutSerializer,
    ProfileUpdateSerializer,
    RegisterSerializer,
    ResendOTPSerializer,
    ResetPasswordSerializer,
    SetTransactionPinSerializer,
    UserSerializer,
    VerifyOTPSerializer,
    VerifyResetTokenSerializer,
)


def _error_response(exc: services.AuthServiceError) -> Response:
    return Response(
        {'detail': exc.message, 'code': exc.code},
        status=exc.status,
    )


class RegisterView(APIView):
    """POST /api/auth/register/ — email + phone, send OTP."""

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user = services.register_user(
                email=serializer.validated_data['email'],
                phone=serializer.validated_data['phone'],
            )
        except services.AuthServiceError as exc:
            return _error_response(exc)

        return Response(
            {
                'message': 'Registration started. A verification code has been sent.',
                'email': user.email,
                'phone': user.phone,
            },
            status=status.HTTP_201_CREATED,
        )


class VerifyOTPView(APIView):
    """POST /api/auth/verify-otp/ — verify 4-digit registration OTP."""

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = VerifyOTPSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user = services.verify_registration_otp(
                email=serializer.validated_data['email'],
                otp=serializer.validated_data['otp'],
            )
        except services.AuthServiceError as exc:
            return _error_response(exc)

        return Response(
            {
                'message': 'Email verified successfully.',
                'is_verified': user.is_verified,
                'email': user.email,
            },
            status=status.HTTP_200_OK,
        )


class ResendOTPView(APIView):
    """POST /api/auth/resend-otp/ — resend registration OTP."""

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = ResendOTPSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.resend_registration_otp(serializer.validated_data['email'])
        except services.AuthServiceError as exc:
            return _error_response(exc)

        return Response(
            {'message': 'A new verification code has been sent.'},
            status=status.HTTP_200_OK,
        )


class CompleteProfileView(APIView):
    """POST /api/auth/complete-profile/ — full name + password; returns JWT."""

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = CompleteProfileSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user, tokens = services.complete_profile(
                email=serializer.validated_data['email'],
                full_name=serializer.validated_data['full_name'],
                password=serializer.validated_data['password'],
            )
        except services.AuthServiceError as exc:
            return _error_response(exc)

        return Response(
            {
                'message': 'Profile completed successfully.',
                'user': UserSerializer(user, context={'request': request}).data,
                'access': tokens['access'],
                'refresh': tokens['refresh'],
            },
            status=status.HTTP_200_OK,
        )


class SetTransactionPinView(APIView):
    """POST /api/auth/set-transaction-pin/ — authenticated 4-digit PIN setup."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = SetTransactionPinSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user = services.set_transaction_pin(
                user=request.user,
                pin=serializer.validated_data['pin'],
            )
        except services.AuthServiceError as exc:
            return _error_response(exc)

        return Response(
            {
                'message': 'Transaction PIN set successfully.',
                'user': UserSerializer(user, context={'request': request}).data,
            },
            status=status.HTTP_200_OK,
        )


class LoginView(APIView):
    """POST /api/auth/login/ — email + password → JWT + user."""

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user, tokens = services.login_user(
                email=serializer.validated_data['email'],
                password=serializer.validated_data['password'],
            )
        except services.AuthServiceError as exc:
            return _error_response(exc)

        return Response(
            {
                'message': 'Login successful.',
                'user': UserSerializer(user, context={'request': request}).data,
                'access': tokens['access'],
                'refresh': tokens['refresh'],
            },
            status=status.HTTP_200_OK,
        )


class LogoutView(APIView):
    """POST /api/auth/logout/ — blacklist refresh token."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.logout_user(serializer.validated_data['refresh'])
        except services.AuthServiceError as exc:
            return _error_response(exc)

        return Response(
            {'message': 'Logged out successfully.'},
            status=status.HTTP_200_OK,
        )


class ChangePasswordView(APIView):
    """POST /api/auth/change-password/ — authenticated password change."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.change_password(
                user=request.user,
                current_password=serializer.validated_data['current_password'],
                new_password=serializer.validated_data['new_password'],
            )
        except services.AuthServiceError as exc:
            return _error_response(exc)

        return Response(
            {'message': 'Password changed successfully.'},
            status=status.HTTP_200_OK,
        )


class ChangeTransactionPinView(APIView):
    """POST /api/auth/change-transaction-pin/ — authenticated PIN change."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = ChangeTransactionPinSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user = services.change_transaction_pin(
                user=request.user,
                current_pin=serializer.validated_data['current_pin'],
                new_pin=serializer.validated_data['new_pin'],
            )
        except services.AuthServiceError as exc:
            return _error_response(exc)

        return Response(
            {
                'message': 'Transaction PIN changed successfully.',
                'user': UserSerializer(user, context={'request': request}).data,
            },
            status=status.HTTP_200_OK,
        )


class ForgotPasswordView(APIView):
    """POST /api/auth/forgot-password/ — send reset OTP (no email enumeration)."""

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.request_password_reset(serializer.validated_data['email'])
        except services.AuthServiceError as exc:
            return _error_response(exc)

        return Response(
            {
                'message': (
                    'If an account exists for this email, '
                    'a password reset code has been sent.'
                )
            },
            status=status.HTTP_200_OK,
        )


class VerifyResetTokenView(APIView):
    """POST /api/auth/verify-reset-token/ — validate password-reset OTP."""

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = VerifyResetTokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.verify_reset_otp(
                email=serializer.validated_data['email'],
                otp=serializer.validated_data['token'],
            )
        except services.AuthServiceError as exc:
            return _error_response(exc)

        return Response(
            {'message': 'Reset code is valid.', 'valid': True},
            status=status.HTTP_200_OK,
        )


class ResetPasswordView(APIView):
    """POST /api/auth/reset-password/ — set new password with OTP token."""

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.reset_password(
                email=serializer.validated_data['email'],
                otp=serializer.validated_data['token'],
                password=serializer.validated_data['password'],
            )
        except services.AuthServiceError as exc:
            return _error_response(exc)

        return Response(
            {'message': 'Password has been reset successfully.'},
            status=status.HTTP_200_OK,
        )


class ProfileView(generics.RetrieveUpdateAPIView):
    """GET / PUT / PATCH /api/profile/"""

    permission_classes = [permissions.IsAuthenticated]
    http_method_names = ['get', 'put', 'patch', 'head', 'options']

    def get_object(self):
        return self.request.user

    def get_serializer_class(self):
        if self.request.method in ('PUT', 'PATCH'):
            return ProfileUpdateSerializer
        return UserSerializer

    def retrieve(self, request, *args, **kwargs):
        serializer = UserSerializer(request.user, context={'request': request})
        return Response(serializer.data)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        serializer = ProfileUpdateSerializer(
            request.user,
            data=request.data,
            partial=partial,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            UserSerializer(request.user, context={'request': request}).data
        )


# Aliases matching original requirements naming
VerifyEmailView = VerifyOTPView
ResendVerificationView = ResendOTPView
TokenRefreshAPIView = TokenRefreshView
