import hashlib
import logging
import re
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, override_settings
from django.utils import timezone

from .models import AuditLog, AuthToken, Notification, OrderEmailActionToken, Payment
from .order_email_security import EmailActionLogFilter
from .services import order_email_actions as actions
from .test_bonus_account import _Base
from .views import _send_order_email


@override_settings(SECURE_SSL_REDIRECT=False,
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    ORDER_NOTIFICATION_EMAILS=['operator@example.com'],
    ORDER_EMAIL_ACTION_BASE_URL='https://autoterra.shop')
class OrderEmailConfirmationTests(_Base):
    url = '/order-action/confirm/'

    def setUp(self):
        super().setUp()
        self.http = Client(enforce_csrf_checks=True)
        self.order = self._order(1000, status='new')
        self.raw = actions.issue_confirm_token(self.order)

    def get(self, raw=None):
        return self.http.get(self.url, {'token': self.raw if raw is None else raw}, secure=True)

    def post(self, raw=None):
        return self.http.post(self.url, {'token': self.raw if raw is None else raw}, secure=True)

    def assert_unchanged(self):
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'new')
        self.assertIsNone(self.order.confirmed_at)
        self.assertEqual(AuditLog.objects.filter(changes__source='email').count(), 0)

    def test_issuance_stores_only_hash_with_expiry_and_order(self):
        row = OrderEmailActionToken.objects.get()
        self.assertEqual(row.order, self.order)
        self.assertEqual(row.action, 'confirm')
        self.assertEqual(row.token_hash, hashlib.sha256(self.raw.encode()).hexdigest())
        self.assertNotIn(self.raw, str(row.__dict__))
        self.assertEqual(len(self.raw), 43)
        self.assertAlmostEqual((row.expires_at - timezone.now()).total_seconds(), 86400, delta=5)

    def test_get_and_ten_link_scanner_visits_have_no_side_effects(self):
        for _ in range(10):
            response = self.get()
            self.assertContains(response, 'Подтвердить заказ')
            self.assertContains(response, self.client_profile.company_name)
            self.assertEqual(response['Referrer-Policy'], 'no-referrer')
            self.assertIn('no-store', response['Cache-Control'])
        self.assert_unchanged()
        self.assertIsNone(OrderEmailActionToken.objects.get().used_at)
        self.assertFalse(Notification.objects.exists())

    def test_post_confirms_once_audits_and_notifies_without_returning_token(self):
        with patch('api.views._send_order_status_email') as email:
            response = self.post()
            self.assertContains(response, f'Заказ ORD-{self.order.pk:05d} подтверждён')
            self.assertNotContains(response, self.raw)
            self.order.refresh_from_db()
            self.assertEqual(self.order.status, 'confirmed')
            self.assertIsNotNone(self.order.confirmed_at)
            self.assertIsNotNone(OrderEmailActionToken.objects.get().used_at)
            self.assertEqual(Notification.objects.filter(title='Заказ подтверждён').count(), 1)
            email.assert_called_once()
            audit = AuditLog.objects.get(changes__source='email')
            self.assertEqual(audit.action, 'Order confirmed from email')
            self.assertEqual(audit.changes['orderId'], self.order.pk)
            self.assertIsNone(audit.user)
            self.assertNotIn(self.raw, str(audit.__dict__))
            self.assertContains(self.post(), 'Заказ уже был обработан по этой ссылке', status_code=409)
            self.assertContains(self.get(), 'Заказ уже был обработан по этой ссылке', status_code=409)
            email.assert_called_once()
            self.assertEqual(Notification.objects.count(), 1)
            self.assertEqual(AuditLog.objects.filter(changes__source='email').count(), 1)

    def test_expired_link_cannot_preview_or_confirm(self):
        OrderEmailActionToken.objects.update(expires_at=timezone.now()-timedelta(seconds=1))
        for response in (self.get(), self.post()):
            self.assertContains(response, 'Срок действия ссылки истёк', status_code=410)
            self.assertNotContains(response, self.raw, status_code=410)
        self.assert_unchanged()

    def test_invalid_random_missing_and_order_id_are_not_capabilities(self):
        for raw in ('', 'random', str(self.order.pk), 'x'*43, '<script>', 'x'*10000):
            for response in (self.get(raw), self.post(raw)):
                self.assertContains(response, 'Ссылка недействительна', status_code=400)
                self.assertNotContains(response, 'name="token"', status_code=400)
        self.assert_unchanged()

    def test_resend_invalidates_old_link_and_retains_only_last_active(self):
        new = actions.issue_confirm_token(self.order)
        self.assertNotEqual(new, self.raw)
        self.assertContains(self.post(), 'Заказ уже был обработан', status_code=409)
        self.assertEqual(OrderEmailActionToken.objects.filter(used_at__isnull=True).count(), 1)
        self.assertEqual(self.post(new).status_code, 200)

    def test_closed_or_adjusted_orders_do_not_repeat_side_effects(self):
        for status in ('confirmed', 'paid', 'shipped', 'fulfilled', 'rejected', 'cancelled', 'adjusted'):
            self.order.status = status
            self.order.save(update_fields=['status'])
            with patch('api.services.orders._notify_event') as notify:
                self.assertContains(self.post(), 'Заказ уже имеет статус:', status_code=409)
                self.assertContains(self.get(), 'Заказ уже имеет статус:', status_code=409)
                notify.assert_not_called()
            self.assertFalse(AuditLog.objects.filter(changes__source='email').exists())
        self.assertIsNone(OrderEmailActionToken.objects.get().used_at)

    def test_pending_or_succeeded_payments_use_shared_guards(self):
        for status in ('pending', 'succeeded'):
            payment = Payment.objects.create(order=self.order, amount=1000, provider='yookassa', status=status)
            self.assertEqual(self.post().status_code, 409)
            self.assert_unchanged()
            payment.delete()

    def test_server_error_is_generic_and_transaction_rolls_back(self):
        with patch('api.services.orders._audit', side_effect=RuntimeError(self.raw)):
            with self.assertLogs('api.order_email_views', level='ERROR') as logs:
                response = self.post()
        self.assertContains(response, 'Не удалось подтвердить заказ.', status_code=500)
        self.assertNotContains(response, self.raw, status_code=500)
        self.assertNotIn(self.raw, str(logs.output))
        self.assert_unchanged()
        self.assertIsNone(OrderEmailActionToken.objects.get().used_at)
        self.assertEqual(self.post().status_code, 200)

    def test_email_contains_button_plain_link_and_new_token_on_resend(self):
        _send_order_email(self.order)
        message = mail.outbox[-1]
        html = message.alternatives[0].content
        self.assertIn('Проверить и подтвердить заказ', html)
        self.assertIn('Ссылка действует 24 часа', html)
        link = re.search(r'https://autoterra.shop/order-action/confirm/\?token=([\w-]+)', message.body)
        self.assertIsNotNone(link)
        self.assertIn(link.group(0), html)
        self.assertEqual(self.get(link.group(1)).status_code, 200)
        _send_order_email(self.order)
        self.assertEqual(self.post(link.group(1)).status_code, 409)

    def test_existing_operator_endpoint_still_works_and_invalidates_email_by_status(self):
        user = User.objects.create_user(username='operator', password='pw')
        user.profile.role = 'distributor'
        user.profile.save()
        self.distributor.user = user
        self.distributor.save()
        auth = AuthToken.objects.create(user=user, key='operator-token')
        url = f'/api/orders/{self.order.pk}/confirm/'
        self.assertIn(self.http.post(url, secure=True).status_code, (401, 403))
        response = self.http.post(url, secure=True, HTTP_AUTHORIZATION=f'Bearer {auth.key}')
        self.assertEqual(response.status_code, 200, response.content)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'confirmed')
        self.assertEqual(self.post().status_code, 409)
        self.assertEqual(Notification.objects.filter(title='Заказ подтверждён').count(), 1)

    def test_token_bound_to_action_and_order(self):
        other = self._order(500, status='new')
        OrderEmailActionToken.objects.update(action='other')
        self.assertEqual(self.post().status_code, 400)
        self.assert_unchanged()
        other.refresh_from_db()
        self.assertEqual(other.status, 'new')

    def test_http_and_unsafe_methods_rejected(self):
        self.assertEqual(self.http.post(self.url, {'token': self.raw}).status_code, 400)
        self.assertEqual(self.http.put(self.url, {'token': self.raw}, secure=True).status_code, 405)
        self.assert_unchanged()

    @override_settings(ORDER_EMAIL_ACTION_BASE_URL='http://autoterra.shop')
    def test_email_never_generates_http_capability(self):
        with self.assertLogs('api.views', level='ERROR'):
            _send_order_email(self.order)
        self.assertFalse(mail.outbox)

    def test_access_log_redacts_token(self):
        record = logging.LogRecord('django.server', logging.INFO, '', 1,
                                  '"GET %s HTTP/1.1" 200', (actions.confirmation_url(self.raw),), None)
        EmailActionLogFilter().filter(record)
        self.assertNotIn(self.raw, record.getMessage())
        self.assertIn('token=[REDACTED]', record.getMessage())

    def test_post_requires_body_token_and_does_not_use_query(self):
        response = self.http.post(self.url + '?token=' + self.raw, secure=True)
        self.assertEqual(response.status_code, 400)
        self.assert_unchanged()

    def test_legacy_accepted_status_can_be_confirmed(self):
        self.order.status = 'accepted'
        self.order.save(update_fields=['status'])
        self.assertEqual(self.post().status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'confirmed')

    def test_page_escapes_client_and_item_content(self):
        self.client_profile.company_name = '<script>alert(1)</script>'
        self.client_profile.save(update_fields=['company_name'])
        self.order.items.update(name='<img src=x onerror=alert(1)>')
        response = self.get()
        self.assertNotContains(response, '<script>')
        self.assertNotContains(response, '<img')
        self.assertContains(response, '&lt;script&gt;')
