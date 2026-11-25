from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from audit.models import AuditEntry


class PasswordChangeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('sara', password='campus123', role=User.Role.REQUESTER)
        self.client.force_login(self.user)

    def test_change_password_keeps_user_signed_in_and_is_audited(self):
        response = self.client.post(reverse('password_change'), {
            'old_password': 'campus123', 'new_password1': 'Green-Chair-42', 'new_password2': 'Green-Chair-42'})
        self.assertRedirects(response, reverse('home'))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('Green-Chair-42'))
        self.assertTrue(self.user.password.startswith('pbkdf2_sha256$'))  # only the hash is stored
        self.assertNotIn('Green-Chair-42', self.user.password)
        self.assertEqual(self.client.get(reverse('home')).status_code, 200)
        self.assertTrue(AuditEntry.objects.filter(action='Password changed').exists())

    def test_wrong_old_password_or_weak_new_password_is_refused(self):
        for data in ({'old_password': 'wrong', 'new_password1': 'Green-Chair-42', 'new_password2': 'Green-Chair-42'},
                     {'old_password': 'campus123', 'new_password1': '12345678', 'new_password2': '12345678'},
                     {'old_password': 'campus123', 'new_password1': 'Green-Chair-42', 'new_password2': 'Other-1'}):
            self.client.post(reverse('password_change'), data)
            self.user.refresh_from_db()
            self.assertTrue(self.user.check_password('campus123'))

    def test_signed_out_user_is_sent_to_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse('password_change')).status_code, 302)
