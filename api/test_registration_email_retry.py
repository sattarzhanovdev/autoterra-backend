from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core import mail
from django.core.management import call_command
from django.utils import timezone

from .models import RegistrationEmailDelivery
from django.test import TestCase, override_settings
from . import test_registration_email as registration


@override_settings(EMAIL_BACKEND=registration.LOCMEM, DEFAULT_FROM_EMAIL="no-reply@autoterra.ru")
class RegistrationEmailRetryTests(TestCase):
    setUp = registration.RegistrationEmailTests.setUp
    _register = registration.RegistrationEmailTests._register

    def test_smtp_failure_is_persisted_and_retried_once(self):
        with patch('django.core.mail.EmailMultiAlternatives.send', side_effect=OSError('SMTP down')):
            self.assertEqual(self._register().status_code, 201)
        item = RegistrationEmailDelivery.objects.get()
        self.assertIsNone(item.sent_at)
        self.assertEqual(item.attempts, 1)
        self.assertEqual(item.last_error, 'OSError')
        call_command('retry_registration_emails', stdout=StringIO())
        item.refresh_from_db()
        self.assertIsNotNone(item.sent_at)
        self.assertEqual(item.attempts, 2)
        self.assertEqual(item.last_error, '')
        self.assertEqual(mail.outbox[0].to, ['zavtoterra@yandex.ru'])
        call_command('retry_registration_emails', stdout=StringIO())
        self.assertEqual(len(mail.outbox), 1)

    def test_console_backend_does_not_mark_email_as_delivered(self):
        with self.settings(EMAIL_BACKEND='django.core.mail.backends.console.EmailBackend'):
            self.assertEqual(self._register().status_code, 201)
        item = RegistrationEmailDelivery.objects.get()
        self.assertIsNone(item.sent_at)
        call_command('retry_registration_emails', stdout=StringIO())
        self.assertEqual(len(mail.outbox), 1)

    def test_worker_skips_live_lease_and_recovers_expired_lease(self):
        with patch('django.core.mail.EmailMultiAlternatives.send', side_effect=OSError()):
            self._register()
        item = RegistrationEmailDelivery.objects.get()
        item.locked_until = timezone.now() + timedelta(minutes=5)
        item.save()
        call_command('retry_registration_emails', stdout=StringIO())
        self.assertEqual(mail.outbox, [])
        item.locked_until = timezone.now() - timedelta(seconds=1)
        item.save()
        call_command('retry_registration_emails', stdout=StringIO())
        self.assertEqual(len(mail.outbox), 1)

    def test_historical_registration_can_be_queued_explicitly(self):
        self._register()
        RegistrationEmailDelivery.objects.all().delete()
        mail.outbox = []
        call_command('retry_registration_emails', stdout=StringIO())
        self.assertEqual(mail.outbox, [])
        call_command('retry_registration_emails', enqueue_since=timezone.localdate().isoformat(), stdout=StringIO())
        self.assertEqual(len(mail.outbox), 1)
