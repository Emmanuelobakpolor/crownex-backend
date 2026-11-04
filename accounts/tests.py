"""Smoke tests for CrownEx authentication pipeline."""

from django.core import mail
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from .models import OTPPurpose, User, VerificationOTP
from .tokens import hash_code, otp_expiry


@override_settings(
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    OTP_RESEND_COOLDOWN_SECONDS=0,
)
class AuthFlowTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.email = 'john@example.com'
        self.phone = '08012345678'
        self.password = 'SecurePass1!'

    def _latest_otp_from_email(self) -> str:
        self.assertTrue(mail.outbox, 'Expected an email to be sent')
        body = mail.outbox[-1].body
        # "Your CrownEx verification code is: 1234"
        for word in body.split():
            if word.isdigit() and len(word) == 4:
                return word
        # Fallback: read from DB by re-creating known code in helper tests
        raise AssertionError(f'Could not parse OTP from email body: {body!r}')

    def _inject_otp(self, user: User, purpose=OTPPurpose.REGISTRATION, code='1234'):
        VerificationOTP.objects.filter(
            user=user, purpose=purpose, is_used=False
        ).update(is_used=True)
        VerificationOTP.objects.create(
            user=user,
            code_hash=hash_code(code),
            purpose=purpose,
            expires_at=otp_expiry(),
        )
        return code

    def test_full_registration_login_profile_reset(self):
        # Step 1: register
        res = self.client.post(
            '/api/auth/register/',
            {'email': self.email, 'phone': self.phone},
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data['email'], self.email)
        user = User.objects.get(email=self.email)
        self.assertFalse(user.is_verified)

        otp = self._latest_otp_from_email()

        # Step 2: verify OTP
        res = self.client.post(
            '/api/auth/verify-otp/',
            {'email': self.email, 'otp': otp},
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertTrue(user.is_verified)

        # Login should fail before profile complete
        res = self.client.post(
            '/api/auth/login/',
            {'email': self.email, 'password': self.password},
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.data['code'], 'profile_incomplete')

        # Step 3: complete profile → JWT
        res = self.client.post(
            '/api/auth/complete-profile/',
            {
                'email': self.email,
                'full_name': 'John Olamide',
                'password': self.password,
                'password_confirm': self.password,
            },
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn('access', res.data)
        self.assertIn('refresh', res.data)
        access = res.data['access']
        refresh = res.data['refresh']

        # Step 4: set transaction PIN
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')
        res = self.client.post(
            '/api/auth/set-transaction-pin/',
            {'pin': '1234', 'pin_confirm': '1234'},
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertTrue(user.has_transaction_pin)
        self.assertTrue(user.check_transaction_pin('1234'))

        # Profile GET
        res = self.client.get('/api/profile/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['full_name'], 'John Olamide')
        self.assertEqual(res.data['email'], self.email)

        # Profile update
        res = self.client.patch(
            '/api/profile/',
            {'full_name': 'John Updated'},
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['full_name'], 'John Updated')

        # Logout
        res = self.client.post(
            '/api/auth/logout/',
            {'refresh': refresh},
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        # Fresh login
        self.client.credentials()
        res = self.client.post(
            '/api/auth/login/',
            {'email': self.email, 'password': self.password},
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn('access', res.data)

        # Password reset flow
        mail.outbox.clear()
        res = self.client.post(
            '/api/auth/forgot-password/',
            {'email': self.email},
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        reset_otp = self._latest_otp_from_email()

        res = self.client.post(
            '/api/auth/verify-reset-token/',
            {'email': self.email, 'token': reset_otp},
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        new_password = 'NewSecurePass1!'
        res = self.client.post(
            '/api/auth/reset-password/',
            {
                'email': self.email,
                'token': reset_otp,
                'password': new_password,
                'password_confirm': new_password,
            },
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        res = self.client.post(
            '/api/auth/login/',
            {'email': self.email, 'password': new_password},
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    def test_duplicate_email_rejected(self):
        User.objects.create_user(
            email=self.email,
            phone=self.phone,
            password=self.password,
            is_verified=True,
            is_profile_complete=True,
            full_name='Existing',
        )
        res = self.client.post(
            '/api/auth/register/',
            {'email': self.email, 'phone': '08099999999'},
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data['code'], 'email_exists')

    def test_unverified_login_blocked(self):
        user = User.objects.create_user(
            email=self.email,
            phone=self.phone,
            password=self.password,
            is_verified=False,
            is_profile_complete=True,
            full_name='John',
        )
        res = self.client.post(
            '/api/auth/login/',
            {'email': self.email, 'password': self.password},
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.data['code'], 'email_not_verified')
        self.assertTrue(User.objects.filter(pk=user.pk).exists())

    def test_invalid_otp(self):
        self.client.post(
            '/api/auth/register/',
            {'email': self.email, 'phone': self.phone},
            format='json',
        )
        res = self.client.post(
            '/api/auth/verify-otp/',
            {'email': self.email, 'otp': '0000'},
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data['code'], 'otp_invalid')

    def test_token_refresh(self):
        user = User.objects.create_user(
            email=self.email,
            phone=self.phone,
            password=self.password,
            is_verified=True,
            is_profile_complete=True,
            full_name='John',
        )
        login = self.client.post(
            '/api/auth/login/',
            {'email': self.email, 'password': self.password},
            format='json',
        )
        refresh = login.data['refresh']
        res = self.client.post(
            '/api/auth/refresh/',
            {'refresh': refresh},
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn('access', res.data)
        self.assertTrue(User.objects.filter(pk=user.pk).exists())

    def test_register_email_failure_returns_503_and_rolls_back(self):
        from smtplib import SMTPServerDisconnected
        from unittest import mock

        with mock.patch(
            'accounts.services.send_otp_email',
            side_effect=SMTPServerDisconnected('Connection unexpectedly closed'),
        ):
            res = self.client.post(
                '/api/auth/register/',
                {'email': self.email, 'phone': self.phone},
                format='json',
            )
        self.assertEqual(res.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(res.data['code'], 'email_send_failed')
        self.assertFalse(User.objects.filter(email=self.email).exists())
        self.assertFalse(VerificationOTP.objects.exists())


@override_settings(RESEND_API_KEY='re_test', DEFAULT_FROM_EMAIL='CrownEx <noreply@crownex.app>')
class ResendBackendTests(TestCase):
    def _send(self, response_status=200, body=None):
        from unittest import mock

        from django.core.mail import send_mail

        fake = mock.Mock(status_code=response_status)
        fake.json.return_value = body or {'id': 'abc'}
        with mock.patch('accounts.resend_backend.requests.post', return_value=fake) as post:
            send_mail(
                subject='Code',
                message='Your code is 1234',
                from_email=None,
                recipient_list=['john@example.com'],
                fail_silently=False,
                connection=self._backend(),
            )
        return post

    def _backend(self):
        from .resend_backend import ResendEmailBackend

        return ResendEmailBackend()

    def test_posts_message_to_resend(self):
        post = self._send()
        args, kwargs = post.call_args
        self.assertEqual(args[0], 'https://api.resend.com/emails')
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer re_test')
        self.assertEqual(kwargs['json']['to'], ['john@example.com'])
        self.assertEqual(kwargs['json']['from'], 'CrownEx <noreply@crownex.app>')
        self.assertEqual(kwargs['json']['text'], 'Your code is 1234')

    def test_api_error_raises(self):
        from .resend_backend import ResendError

        with self.assertRaisesMessage(ResendError, 'domain is not verified'):
            self._send(403, {'message': 'The crownex.app domain is not verified.'})
