from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User

from .models import SupportConversation


class SupportChatTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email='user@example.com', password='pass12345', full_name='Ada Obi'
        )
        self.admin = User.objects.create_superuser(
            email='agent@example.com', password='pass12345', full_name='Tunde Bello'
        )

    def as_user(self):
        self.client.force_authenticate(self.user)

    def as_admin(self):
        self.client.force_authenticate(self.admin)

    def test_full_round_trip(self):
        self.as_user()
        res = self.client.get('/api/support/conversation/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['messages'], [])
        conv_id = res.data['id']

        res = self.client.post('/api/support/messages/', {'body': '  Deposit not credited  '})
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data['body'], 'Deposit not credited')
        first_id = res.data['id']

        # Admin inbox shows the thread with one unread message.
        self.as_admin()
        res = self.client.get('/api/admin/support/conversations/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['unread_total'], 1)
        row = res.data['results'][0]
        self.assertEqual(row['id'], conv_id)
        self.assertEqual(row['unread_count'], 1)
        self.assertEqual(row['last_message']['body'], 'Deposit not credited')

        # Opening it marks it read.
        res = self.client.get(f'/api/admin/support/conversations/{conv_id}/')
        self.assertEqual(len(res.data['messages']), 1)
        res = self.client.get('/api/admin/support/conversations/')
        self.assertEqual(res.data['unread_total'], 0)

        # Admin typing shows up for the user, then clears on send.
        self.client.post(f'/api/admin/support/conversations/{conv_id}/typing/', {'typing': True})
        self.as_user()
        self.assertEqual(self.client.get('/api/support/unread/').data['unread'], 0)
        res = self.client.get(f'/api/support/conversation/?after={first_id}')
        self.assertTrue(res.data['admin_typing'])
        self.assertEqual(res.data['messages'], [])

        self.as_admin()
        res = self.client.post(
            f'/api/admin/support/conversations/{conv_id}/messages/', {'body': 'Checking now'}
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data['sender_name'], 'Tunde')

        self.as_user()
        self.assertEqual(self.client.get('/api/support/unread/').data['unread'], 1)
        res = self.client.get(f'/api/support/conversation/?after={first_id}')
        self.assertFalse(res.data['admin_typing'])
        self.assertEqual([m['body'] for m in res.data['messages']], ['Checking now'])
        self.assertIsNotNone(res.data['admin_last_read_at'])
        self.assertEqual(self.client.get('/api/support/unread/').data['unread'], 0)

    def test_typing_expires(self):
        conv = SupportConversation.objects.create(
            user=self.user, admin_typing_at=timezone.now() - timedelta(seconds=30)
        )
        self.assertFalse(conv.admin_is_typing)

    def test_user_message_reopens_resolved_chat(self):
        conv = SupportConversation.objects.create(user=self.user, status='resolved')
        self.as_user()
        self.client.post('/api/support/messages/', {'body': 'Still broken'})
        conv.refresh_from_db()
        self.assertEqual(conv.status, 'open')

    def test_non_staff_cannot_use_admin_inbox(self):
        self.as_user()
        res = self.client.get('/api/admin/support/conversations/')
        self.assertEqual(res.status_code, 403)

    def test_empty_message_rejected(self):
        self.as_user()
        res = self.client.post('/api/support/messages/', {'body': '   '})
        self.assertEqual(res.status_code, 400)
