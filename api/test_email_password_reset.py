import json
from datetime import timedelta
from io import StringIO
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from api.models import AuthToken, Distributor, PasswordResetRequest
from api.password_reset import deliver_reset_emails


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    PASSWORD_RESET_URL="https://avtoterraplus.ru/reset-password.html",
    DEFAULT_FROM_EMAIL="accounts@avtoterraplus.ru",
)
class PasswordResetTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("+79991234567", "client@example.com", "Original!Password-835")

    def post(self, path, data):
        return self.client.post(path, json.dumps(data), content_type="application/json")

    def request_reset(self, email="client@example.com"):
        return self.post("/api/auth/password-reset/", {"email": email})

    def token(self):
        self.request_reset()
        self.assertEqual(deliver_reset_emails(), 1)
        url = next(line for line in mail.outbox[-1].body.splitlines() if line.startswith("https://"))
        return parse_qs(urlsplit(url).query)["token"][0]

    def confirm(self, token, password="Changed!Password-943"):
        return self.post("/api/auth/password-reset/confirm/", {"token": token, "new_password": password})

    def test_end_to_end_revokes_tokens_and_all_reset_links(self):
        self.user.profile.role = "distributor"
        self.user.profile.save(update_fields=["role"])
        Distributor.objects.create(user=self.user, name="Test", inn="1234567890", phone=self.user.username, email=self.user.email)
        AuthToken.objects.create(user=self.user, key="old-session")
        legacy_code = default_token_generator.make_token(self.user)
        token = self.token()
        self.request_reset()
        deliver_reset_emails()
        self.assertEqual(self.confirm(token).status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("Changed!Password-943"))
        self.assertFalse(default_token_generator.check_token(self.user, legacy_code))
        self.assertFalse(AuthToken.objects.filter(user=self.user).exists())
        self.assertFalse(PasswordResetRequest.objects.filter(user=self.user, consumed_at__isnull=True).exists())
        self.assertEqual(self.confirm(token).status_code, 400)
        old_login = self.post("/api/login/", {"phone": self.user.username, "password": "Original!Password-835"})
        new_login = self.post("/api/login/", {"phone": self.user.username, "password": "Changed!Password-943"})
        self.assertEqual(old_login.status_code, 401)
        self.assertEqual(new_login.status_code, 200)

    def test_queue_response_does_not_expose_account_or_token(self):
        existing = self.request_reset()
        unknown = self.request_reset("unknown@example.com")
        self.assertEqual(existing.status_code, 202)
        self.assertEqual(existing.json(), unknown.json())
        self.assertEqual(PasswordResetRequest.objects.count(), 1)
        self.assertFalse(hasattr(mail, "outbox") and mail.outbox)
        self.assertEqual(deliver_reset_emails(), 1)
        url = next(line for line in mail.outbox[-1].body.splitlines() if line.startswith("https://"))
        token = parse_qs(urlsplit(url).query)["token"][0]
        job = PasswordResetRequest.objects.filter(status="sent").last()
        self.assertNotEqual(job.token_hash, token)
        self.assertNotIn(token, existing.content.decode())

    def test_case_insensitive_email_sends_to_stored_address(self):
        self.assertEqual(self.request_reset(" CLIENT@EXAMPLE.COM ").status_code, 202)
        call_command("send_password_reset_emails", stdout=StringIO())
        self.assertEqual(mail.outbox[0].to, [self.user.email])
        self.assertEqual(mail.outbox[0].from_email, "accounts@avtoterraplus.ru")

    def test_expired_and_tampered_tokens_are_rejected(self):
        token = self.token()
        self.assertEqual(self.confirm("0" * 64).status_code, 400)
        PasswordResetRequest.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.confirm(token).status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("Original!Password-835"))

    def test_password_policy_and_same_password_do_not_consume_link(self):
        token = self.token()
        for password in ["12345678", "short", "", "Original!Password-835", "x" * 129]:
            response = self.confirm(token, password)
            self.assertEqual(response.status_code, 400)
            self.assertIn("errors", response.json())
        self.assertEqual(self.confirm(token).status_code, 200)

    def test_email_or_password_changes_invalidate_link(self):
        token = self.token()
        self.user.email = "changed@example.com"
        self.user.save()
        self.assertEqual(self.confirm(token).status_code, 400)
        self.user.email = "client@example.com"
        self.user.set_password("ChangedElsewhere!447")
        self.user.save()
        self.assertEqual(self.confirm(token).status_code, 400)

    def test_inactive_unusable_and_duplicate_accounts_do_not_receive_mail(self):
        self.user.is_active = False
        self.user.save()
        self.request_reset()
        self.user.is_active = True
        self.user.set_unusable_password()
        self.user.save()
        self.request_reset()
        self.user.set_password("Original!Password-835")
        self.user.save()
        User.objects.create_user("another", "CLIENT@example.com", "Other!Password-441")
        self.request_reset()
        self.assertEqual(PasswordResetRequest.objects.count(), 0)

    def test_account_disabled_after_request_or_delivery(self):
        self.request_reset()
        self.user.is_active = False
        self.user.save()
        self.assertEqual(deliver_reset_emails(), 0)
        self.user.is_active = True
        self.user.save()
        token = self.token()
        self.user.is_active = False
        self.user.save()
        self.assertEqual(self.confirm(token).status_code, 400)

    def test_email_limit_silently_suppresses_excess_requests(self):
        for _ in range(5):
            self.assertEqual(self.request_reset().status_code, 202)
        self.assertEqual(PasswordResetRequest.objects.count(), 3)

    def test_ip_limit_covers_unknown_addresses(self):
        for i in range(20):
            self.assertEqual(self.request_reset(f"unknown{i}@example.com").status_code, 202)
        response = self.request_reset("other@example.com")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["Retry-After"], "3600")

    def test_confirm_attempt_limit(self):
        for _ in range(60):
            self.assertEqual(self.confirm("0" * 64).status_code, 400)
        self.assertEqual(self.confirm("0" * 64).status_code, 429)

    def test_mail_failure_is_retried_without_leaking_token(self):
        self.request_reset()
        with patch("api.password_reset.send_mail", side_effect=OSError("SMTP unavailable")):
            self.assertEqual(deliver_reset_emails(), 0)
        job = PasswordResetRequest.objects.get()
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.token_hash, "")
        self.assertEqual(deliver_reset_emails(), 1)
        self.assertEqual(deliver_reset_emails(), 0)

    def test_failed_delivery_stops_after_three_attempts(self):
        self.request_reset()
        with patch("api.password_reset.send_mail", return_value=0) as send:
            for _ in range(4):
                deliver_reset_emails()
            self.assertEqual(send.call_count, 3)

    def test_existing_smtp_transport_is_used(self):
        self.request_reset()
        with self.settings(EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
                           EMAIL_HOST="existing-smtp.example.com", EMAIL_PORT=587,
                           EMAIL_HOST_USER="existing-account", EMAIL_HOST_PASSWORD="test-only"):
            with patch("django.core.mail.backends.smtp.EmailBackend.send_messages", return_value=1) as smtp:
                self.assertEqual(deliver_reset_emails(), 1)
        message = smtp.call_args.args[0][0]
        self.assertEqual(message.to, [self.user.email])
        self.assertEqual(message.from_email, "accounts@avtoterraplus.ru")
        self.assertIn("?token=", message.body)
        self.assertNotIn("test-only", message.body)

    def test_non_delivery_backends_do_not_mark_sent(self):
        self.request_reset()
        for backend in ("console", "dummy", "filebased"):
            with self.settings(EMAIL_BACKEND=f"django.core.mail.backends.{backend}.EmailBackend"):
                self.assertEqual(deliver_reset_emails(), 0)
                self.assertEqual(PasswordResetRequest.objects.get().status, "failed")

    def test_existing_mobile_reset_invalidates_new_link(self):
        self.user.profile.role = "distributor"
        self.user.profile.save(update_fields=["role"])
        Distributor.objects.create(user=self.user, name="Test", inn="1234567890")
        token = self.token()
        with self.settings(PASSWORD_RESET_URL=""):
            response = self.post("/api/auth/password-reset/", {
                "phone": self.user.username, "inn": "1234567890", "new_password": "LegacyPassword!931",
                "code": default_token_generator.make_token(self.user),
            })
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.confirm(token).status_code, 400)

    def test_worker_recovers_stale_claim_and_ignores_old_requests(self):
        self.request_reset()
        PasswordResetRequest.objects.update(status="sending", claimed_at=timezone.now() - timedelta(minutes=6))
        self.assertEqual(deliver_reset_emails(), 1)
        self.request_reset()
        PasswordResetRequest.objects.update(created_at=timezone.now() - timedelta(hours=2))
        self.assertEqual(deliver_reset_emails(), 0)

    def test_invalid_json_types_and_methods(self):
        for path in ["/api/auth/password-reset/", "/api/auth/password-reset/confirm/"]:
            self.assertEqual(self.client.get(path).status_code, 405)
            for value in [[], "hello", None, 5, {"email": 5}, {"token": [], "new_password": 7}]:
                self.assertEqual(self.post(path, value).status_code, 400)
            self.assertEqual(self.client.post(path, b"{", content_type="application/json").status_code, 400)
        self.assertEqual(self.request_reset("not-an-email").status_code, 400)

    def test_unconfigured_url_disables_requests(self):
        for url in ["", "http://example.com/reset", "https://example.com/reset?next=evil"]:
            with self.settings(PASSWORD_RESET_URL=url):
                self.assertEqual(self.request_reset().status_code, 503)

    @override_settings(ALLOWED_HOSTS=["testserver", "evil.example"])
    def test_untrusted_host_does_not_change_link(self):
        self.client.post("/api/auth/password-reset/", json.dumps({"email": self.user.email}),
                         content_type="application/json", HTTP_HOST="evil.example")
        deliver_reset_emails()
        self.assertIn("https://avtoterraplus.ru/reset-password.html?token=", mail.outbox[0].body)
        self.assertNotIn("evil.example", mail.outbox[0].body)
