from decimal import Decimal
from unittest.mock import patch

from django.core import mail
from django.test import Client, override_settings

from .models import AuditLog, Notification, OrderAdjustment, OrderEmailActionToken, OrderItem, Product
from .services import order_email_actions as actions
from .test_bonus_account import _Base


@override_settings(SECURE_SSL_REDIRECT=False,
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    ORDER_NOTIFICATION_EMAILS=['operator@example.com'],
    ORDER_EMAIL_ACTION_BASE_URL='https://autoterra.shop')
class OrderEmailAdjustmentTests(_Base):
    url = '/order-action/confirm/'

    def setUp(self):
        super().setUp()
        self.http = Client(enforce_csrf_checks=True)
        self.client_profile.user.email = 'client@example.test'
        self.client_profile.user.save(update_fields=['email'])
        self.order = self._order(1000, status='new')
        self.item = self.order.items.get()
        self.item.quantity, self.item.reserved_quantity = 5, 3
        self.item.save(update_fields=['quantity', 'reserved_quantity'])
        self.product.quantity, self.product.status = 0, 'low'
        self.product.save(update_fields=['quantity', 'status'])
        self.raw = actions.issue_confirm_token(self.order)
        self.revision = actions.order_revision(self.order)
        self.push = patch('api.services.push_notifications.PushNotificationService.send')
        self.push.start()
        self.addCleanup(self.push.stop)

    def form(self, quantity='3', **extra):
        return {'token': self.raw, 'revision': self.revision,
                f'quantity_{self.item.pk}': quantity, **extra}

    def submit(self, quantity='3', **extra):
        return self.http.post(self.url, self.form(quantity, **extra), secure=True)

    def assert_unchanged(self):
        self.order.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual((self.order.status, self.item.quantity, self.item.reserved_quantity), ('new', 5, 3))
        self.assertIsNone(OrderEmailActionToken.objects.get(token_hash=actions.token_hash(self.raw)).used_at)
        self.assertFalse(OrderAdjustment.objects.filter(order=self.order).exists())
        self.assertFalse(Notification.objects.exists())

    def test_preview_shows_full_editor_without_changing_order(self):
        for index in range(7):
            product = Product.objects.create(distributor=self.distributor, sku=f'EXTRA-{index}', name=f'Материал {index}', quantity=10, price=100)
            OrderItem.objects.create(order=self.order, product=product, name=product.name, sku=product.sku, price=100, quantity=1)
        response = self.http.get(self.url, {'token': self.raw}, secure=True)
        self.assertContains(response, 'Заказано: 5 шт.')
        self.assertContains(response, 'Доступно с учётом резерва: 3 шт.')
        self.assertContains(response, 'Материал 6')
        self.assertContains(response, 'Количество к поставке', count=8)
        self.assertContains(response, 'name="revision"')
        self.assertEqual(response['Referrer-Policy'], 'no-referrer')
        self.assertIn("form-action 'self'", response['Content-Security-Policy'])
        self.assert_unchanged()

    def test_five_to_three_notifies_client_and_waits_for_acceptance(self):
        response = self.submit(reason='В наличии только 3 шт.')
        self.assertContains(response, 'Корректировка заказа')
        self.assertContains(response, 'отправлена клиенту')
        self.assertNotContains(response, self.raw)
        self.order.refresh_from_db()
        self.item.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual((self.order.status, self.item.quantity, self.item.reserved_quantity, self.product.quantity), ('adjusted', 3, 3, 0))
        self.assertIsNone(self.order.confirmed_at)
        self.assertEqual(self.order.total_amount, Decimal('3000'))
        history = self.order.adjustments.get()
        self.assertEqual(history.original_items[0]['quantity'], 5)
        self.assertEqual(history.adjusted_items[0]['quantity'], 3)
        self.assertEqual(history.reason, 'В наличии только 3 шт.')
        notice = Notification.objects.get(user=self.client_profile.user)
        self.assertEqual(notice.title, 'Заказ скорректирован')
        self.assertIn('было 5 шт., доступно 3 шт.', notice.body)
        self.assertIn('3000', notice.body)
        self.assertEqual(notice.related_link, f'/orders/{self.order.pk}')
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertEqual(email.to, ['client@example.test'])
        self.assertIn('было 5 шт., доступно 3 шт.', email.body)
        self.assertIn('В наличии только 3 шт.', email.body)
        self.assertIn('доступно <strong>3 шт.</strong>', email.alternatives[0].content)
        self.assertIn('До вашего согласия оплата недоступна.', email.body)
        self.assertEqual(AuditLog.objects.get(object_id=str(self.order.pk), changes__source='email').action, 'Order adjust: new -> adjusted')
        self.assertIsNotNone(OrderEmailActionToken.objects.get().used_at)
        # Mobile clients already know how to show and accept these snapshots.
        auth = {'HTTP_AUTHORIZATION': f'Bearer {self.token}'}
        detail = self.http.get(f'/api/orders/{self.order.pk}/', secure=True, **auth).json()['order']
        self.assertFalse(detail['isPayable'])
        self.assertEqual(detail['items'][0]['quantity'], 3)
        payment = self.http.post(f'/api/orders/{self.order.pk}/pay/', {}, content_type='application/json', secure=True, **auth)
        self.assertEqual(payment.status_code, 400)
        accepted = self.http.post(f'/api/orders/{self.order.pk}/accept-adjustment/', secure=True, **auth)
        self.assertEqual(accepted.status_code, 200, accepted.content)
        self.assertTrue(accepted.json()['order']['isPayable'])
        self.assertEqual(accepted.json()['order']['items'][0]['quantity'], 3)

    def test_double_post_never_repeats_adjustment_or_notifications(self):
        self.assertEqual(self.submit().status_code, 200)
        self.assertEqual(self.submit().status_code, 409)
        self.assertEqual(self.order.adjustments.count(), 1)
        self.assertEqual(Notification.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_unchanged_quantities_confirm_without_adjustment_or_repricing(self):
        self.item.reserved_quantity = 5
        self.item.save(update_fields=['reserved_quantity'])
        response = self.submit('5')
        self.assertContains(response, 'подтверждён')
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'confirmed')
        self.assertFalse(self.order.adjustments.exists())
        self.assertEqual(self.order.total_amount, Decimal('5000'))
        self.assertEqual(Notification.objects.get().title, 'Заказ подтверждён')

    def test_reservations_are_released_once_when_reducing(self):
        self.item.reserved_quantity = 5
        self.item.save(update_fields=['reserved_quantity'])
        self.assertEqual(self.submit().status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, 2)
        self.assertEqual(self.submit().status_code, 409)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, 2)

    def test_legacy_unreserved_line_reserves_only_the_corrected_quantity(self):
        self.item.reserved_quantity = 0
        self.item.save(update_fields=['reserved_quantity'])
        self.product.quantity = 3
        self.product.save(update_fields=['quantity'])
        self.assertEqual(self.submit().status_code, 200)
        self.item.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual((self.item.quantity, self.item.reserved_quantity, self.product.quantity), (3, 3, 0))

    def test_invalid_quantity_and_increase_keep_link_and_entered_values(self):
        for value in ('-1', '1.5', 'abc', '', '6', '1000001'):
            with self.subTest(value=value):
                response = self.submit(value)
                self.assertEqual(response.status_code, 400)
                self.assertContains(response, 'name="token"', status_code=400)
                self.assert_unchanged()
        self.assertEqual(self.submit().status_code, 200)

    def test_zero_can_remove_a_line_but_not_empty_the_order(self):
        response = self.submit('0')
        self.assertContains(response, 'Нельзя убрать все позиции.', status_code=400)
        self.assert_unchanged()
        product = Product.objects.create(distributor=self.distributor, sku='KEEP', name='Оставить', price=100, quantity=1)
        item = OrderItem.objects.create(order=self.order, product=product, sku='KEEP', name='Оставить', price=100, quantity=1, reserved_quantity=1)
        data = self.form('0', revision=actions.order_revision(self.order))
        data[f'quantity_{item.pk}'] = '1'
        response = self.http.post(self.url, data, secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.order.items.filter(pk=self.item.pk).exists())
        self.assertIn('было 5 шт., доступно 0 шт.', Notification.objects.get().body)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, 3)

    def test_foreign_or_incomplete_item_set_rejected_atomically(self):
        other = self._order(1000, status='new')
        for data in (dict(self.form(), **{f'quantity_{other.items.get().pk}': '1'}),
                     {'token': self.raw, 'revision': self.revision}):
            response = self.http.post(self.url, data, secure=True)
            self.assertEqual(response.status_code, 409)
            self.assert_unchanged()
        other.refresh_from_db()
        self.assertEqual(other.status, 'new')

    def test_price_or_items_changed_after_preview_need_reopen(self):
        self.item.price = Decimal('900')
        self.item.save(update_fields=['price'])
        self.assertContains(self.submit(), 'Состав заказа изменился.', status_code=409)
        self.assert_unchanged()
        self.assertEqual(self.submit(revision=actions.order_revision(self.order)).status_code, 200)

    def test_unavailable_quantity_cannot_be_confirmed_and_error_allows_retry(self):
        response = self.submit('5')
        self.assertContains(response, 'доступно 3 шт.', status_code=400)
        self.assert_unchanged()
        self.assertEqual(self.submit().status_code, 200)

    def test_under_order_stock_is_exempt_from_physical_stock_limit(self):
        self.product.status = 'onOrder'
        self.product.save(update_fields=['status'])
        response = self.submit('5')
        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'confirmed')

    def test_duplicate_form_fields_and_bad_revision_are_rejected(self):
        data = self.form()
        data[f'quantity_{self.item.pk}'] = ['1', '3']
        self.assertEqual(self.http.post(self.url, data, secure=True).status_code, 400)
        for revision in ('', 'bad', 'я' * 64):
            self.assertEqual(self.submit(revision=revision).status_code, 409)
        self.assert_unchanged()

    def test_comments_are_escaped_in_editor_and_client_email(self):
        comment = '<script>alert(1)</script>'
        response = self.submit(reason=comment)
        self.assertEqual(response.status_code, 200)
        html = mail.outbox[0].alternatives[0].content
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)

    def test_service_error_rolls_back_items_reservations_token_and_notifications(self):
        with patch('api.services.orders._audit', side_effect=RuntimeError(self.raw)):
            with self.assertLogs('api.order_email_views', level='ERROR') as logs:
                response = self.submit()
        self.assertEqual(response.status_code, 500)
        self.assertNotIn(self.raw, response.content.decode())
        self.assertNotIn(self.raw, str(logs.output))
        self.assert_unchanged()
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, 0)
