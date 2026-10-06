from datetime import timedelta
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from unittest import skipUnless

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import connection, close_old_connections
from django.test import Client, TransactionTestCase
from django.utils import timezone

from .models import AuthToken, AuditLog, ClientProfile, CourierTask, Distributor, Order, Payment, client_turnover
from .test_bonus_account import _Base


class CashPaymentTests(_Base):
    def setUp(self):
        super().setUp()
        for role in ('admin', 'distributor', 'courier', 'manager'):
            user = User.objects.create_user(username=role)
            user.profile.role = role
            user.profile.save()
            AuthToken.objects.create(user=user, key=role)
            if role == 'distributor':
                self.distributor.user = user
                self.distributor.save()
            if role == 'courier':
                self.courier = user
        self.permission_url = f'/api/clients/{self.client_profile.pk}/cash-permission/'
        self.order = self._order(1000)
        self.cash_url = f'/api/orders/{self.order.pk}/cash/'

    def auth(self, key='client-token'):
        return {'HTTP_AUTHORIZATION': f'Bearer {key}'}

    def permit(self, value=True, key='distributor'):
        return self.http.patch(self.permission_url, {'cashPaymentAllowed': value}, content_type='application/json', **self.auth(key))

    def choose(self, key='client-token'):
        return self.http.post(self.cash_url, content_type='application/json', **self.auth(key))

    def ready(self):
        self.permit()
        self.assertEqual(self.choose().status_code, 200)
        self.order.refresh_from_db()
        self.order.courier = self.courier
        self.order.save(update_fields=['courier'])
        task = CourierTask.objects.get(order=self.order)
        for item in self.order.items.all():
            response = self.http.post(f'/api/courier/tasks/{task.pk}/items/{item.pk}/pick/', **self.auth('courier'))
            self.assertEqual(response.status_code, 200, response.content)
        response = self.http.post(f'/api/courier/tasks/{task.pk}/status/', {'status': 'in_progress'}, content_type='application/json', **self.auth('courier'))
        self.assertEqual(response.status_code, 200, response.content)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'shipped')
        return task

    def collect(self, task, key='courier'):
        return self.http.post(f'/api/courier/tasks/{task.pk}/collect-cash/', {'amount': '0.01'}, content_type='application/json', **self.auth(key))

    def test_cash_denied_by_default_and_direct_request_cannot_bypass(self):
        self.assertFalse(self.client_profile.cash_payment_allowed)
        self.assertEqual(self.choose().status_code, 403)
        self.assertFalse(CourierTask.objects.filter(order=self.order).exists())

    def test_admin_and_own_distributor_can_change_permission_and_audit(self):
        for role in ('distributor', 'admin'):
            self.assertEqual(self.permit(True, role).status_code, 200)
            self.assertTrue(self.http.get(self.permission_url, **self.auth(role)).json()['cashPaymentAllowed'])
            self.assertEqual(self.permit(False, role).status_code, 200)
        logs = AuditLog.objects.filter(action='Client cash permission updated')
        self.assertEqual(logs.count(), 4)
        self.assertEqual(logs.first().changes['cashPaymentAllowed'], False)

    def test_client_courier_manager_cannot_grant_cash(self):
        for key in ('client-token', 'courier', 'manager'):
            self.assertEqual(self.permit(key=key).status_code, 403)
        self.assertEqual(self.http.patch(self.permission_url).status_code, 401)

    def test_foreign_distributor_cannot_change_or_choose(self):
        user = User.objects.create_user(username='foreign')
        user.profile.role = 'distributor'
        user.profile.save()
        Distributor.objects.create(user=user, name='Foreign', inn='8888888888')
        AuthToken.objects.create(user=user, key='foreign')
        self.assertEqual(self.permit(key='foreign').status_code, 404)
        self.assertEqual(self.choose('foreign').status_code, 404)

    def test_permission_validates_boolean(self):
        for value in ('true', 1, None, {}, []):
            self.assertEqual(self.permit(value).status_code, 400)

    def test_selection_creates_assembly_once_without_marking_paid(self):
        self.permit()
        for _ in range(2):
            response = self.choose()
            self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(CourierTask.objects.filter(order=self.order).count(), 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_method, 'cash')
        self.assertEqual(self.order.status, 'confirmed')
        self.assertIsNone(self.order.paid_at)
        self.assertFalse(self.order.payments.exists())
        self.assertFalse(response.json()['order']['isPayable'])
        self.assertEqual(AuditLog.objects.filter(action='Order cash selected').count(), 1)

    def test_distributor_can_select_for_enabled_client(self):
        self.permit()
        self.assertEqual(self.choose('distributor').status_code, 200)

    def test_only_confirmed_courier_order_can_choose_cash(self):
        self.permit()
        for status in ('new', 'adjusted', 'paid', 'cancelled', 'shipped', 'fulfilled'):
            Order.objects.filter(pk=self.order.pk).update(status=status)
            self.assertEqual(self.choose().status_code, 409)
        Order.objects.filter(pk=self.order.pk).update(status='confirmed', delivery_method='self_pickup')
        self.assertEqual(self.choose().status_code, 409)

    def test_pending_and_succeeded_online_payments_block_cash(self):
        self.permit()
        payment = Payment.objects.create(order=self.order, amount=1000)
        for status in ('pending', 'waiting_for_capture', 'succeeded'):
            payment.status = status
            payment.save()
            self.assertEqual(self.choose().status_code, 409)
        payment.status = 'canceled'
        payment.save()
        self.assertEqual(self.choose().status_code, 200)

    def test_online_payment_is_blocked_after_cash_selection(self):
        self.permit()
        self.choose()
        result = self.http.post(f'/api/orders/{self.order.pk}/pay/', {}, content_type='application/json', **self.auth())
        self.assertEqual(result.status_code, 409, result.content)
        self.assertFalse(self.order.payments.exists())

    def test_full_cash_delivery_and_server_amount_and_idempotence(self):
        task = self.ready()
        self.assertEqual(client_turnover(self.client_profile), 0)
        url = f'/api/courier/tasks/{task.pk}/status/'
        result = self.http.post(url, {'status': 'delivered'}, content_type='application/json', **self.auth('courier'))
        self.assertEqual(result.status_code, 409)
        for _ in range(2):
            result = self.collect(task)
            self.assertEqual(result.status_code, 200, result.content)
            self.assertTrue(result.json()['task']['cashCollected'])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'shipped')
        self.assertIsNotNone(self.order.paid_at)
        self.assertEqual(self.order.cash_collected_by, self.courier)
        payment = self.order.payments.get()
        self.assertEqual(payment.amount, Decimal('1000'))
        self.assertEqual(payment.provider, 'cash')
        self.assertEqual(client_turnover(self.client_profile), Decimal('1000'))
        self.assertEqual(AuditLog.objects.filter(action='Order cash collected').count(), 1)
        result = self.http.post(url, {'status': 'delivered'}, content_type='application/json', **self.auth('courier'))
        self.assertEqual(result.status_code, 200, result.content)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'fulfilled')

    def test_disabling_permission_preserves_selected_orders_but_blocks_new_choices(self):
        task = self.ready()
        self.permit(False)
        self.assertEqual(self.collect(task).status_code, 200)
        second = self._order(500)
        self.assertEqual(self.http.post(f'/api/orders/{second.pk}/cash/', **self.auth()).status_code, 403)

    def test_only_assigned_driver_can_collect(self):
        task = self.ready()
        for role in ('client-token', 'admin', 'distributor', 'manager'):
            self.assertEqual(self.collect(task, role).status_code, 403)
        other = User.objects.create_user(username='other-driver')
        other.profile.role = 'courier'
        other.profile.save()
        AuthToken.objects.create(user=other, key='other-driver')
        self.assertEqual(self.collect(task, 'other-driver').status_code, 403)
        self.assertFalse(self.order.payments.exists())

    def test_cannot_collect_cash_before_departure_or_for_online_order(self):
        self.permit()
        self.choose()
        self.order.refresh_from_db()
        self.order.courier = self.courier
        self.order.save()
        task = CourierTask.objects.get(order=self.order)
        self.assertEqual(self.collect(task).status_code, 409)
        Order.objects.filter(pk=self.order.pk).update(payment_method='online', status='shipped')
        CourierTask.objects.filter(pk=task.pk).update(status='in_progress')
        self.assertEqual(self.collect(task).status_code, 409)

    def test_operator_cannot_bypass_cash_receipt_with_delivery_status(self):
        task = self.ready()
        for url in (f'/api/distributor/delivery-tasks/{task.pk}/status/', f'/api/distributor/orders/{self.order.pk}/status/'):
            value = 'delivered' if 'delivery-tasks' in url else 'fulfilled'
            response = self.http.post(url, {'status': value}, content_type='application/json', **self.auth('distributor'))
            self.assertIn(response.status_code, (400, 409), response.content)

    def test_confirmed_cash_reservation_is_not_expired(self):
        self.permit()
        self.choose()
        old = timezone.now() - timedelta(days=10)
        Order.objects.filter(pk=self.order.pk).update(confirmed_at=old, created_at=old)
        call_command('expire_unpaid_orders', stdout=StringIO())
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'confirmed')

    def test_cancel_cash_order_cancels_assembly(self):
        self.permit()
        self.choose()
        response = self.http.post(f'/api/orders/{self.order.pk}/cancel/', **self.auth())
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(CourierTask.objects.get(order=self.order).status, 'cancelled')

    def test_cash_can_be_chosen_at_checkout_but_assembly_waits_for_confirmation(self):
        payload = {"items": [{"productId": self.product.pk, "quantity": 2}], "paymentMethod": "cash"}
        response = self.http.post('/api/orders/create/', payload, content_type='application/json', **self.auth())
        self.assertEqual(response.status_code, 403)
        self.permit()
        response = self.http.post('/api/orders/create/', payload, content_type='application/json', **self.auth())
        self.assertEqual(response.status_code, 201, response.content)
        order = Order.objects.get(pk=response.json()['order']['id'])
        self.assertEqual(order.payment_method, 'cash')
        self.assertFalse(order.courier_tasks.exists())
        self.permit(False)
        response = self.http.post(f'/api/orders/{order.pk}/confirm/', **self.auth('distributor'))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(order.courier_tasks.count(), 1)
        self.assertFalse(order.payments.exists())

    def test_permission_can_be_set_for_newly_registered_client(self):
        self.client_profile.status = 'new'
        self.client_profile.save()
        self.assertEqual(self.permit(key='admin').status_code, 200)
        self.assertEqual(self.choose().status_code, 403)

    def test_other_client_cannot_select_cash_for_this_order(self):
        other = self._client('another-client', '9876543210')
        AuthToken.objects.create(user=other.user, key='other-client')
        self.permit()
        self.assertEqual(self.choose('other-client').status_code, 404)

    def test_cash_adjustment_requires_acceptance_before_assembly(self):
        self.order.payment_method = 'cash'
        self.order.status = 'adjusted'
        self.order.save()
        self.assertFalse(self.order.courier_tasks.exists())
        response = self.http.post(f'/api/orders/{self.order.pk}/accept-adjustment/', **self.auth())
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.order.courier_tasks.count(), 1)


@skipUnless(connection.vendor == 'postgresql', 'Row locking requires PostgreSQL')
class CashCollectionConcurrencyTests(TransactionTestCase):
    def test_two_receipt_requests_create_one_payment(self):
        # Reuse fixture helpers without inheriting its test cases.
        fixture = CashPaymentTests('test_full_cash_delivery_and_server_amount_and_idempotence')
        fixture.setUp()
        task = fixture.ready()
        def collect():
            close_old_connections()
            try:
                return Client().post(f'/api/courier/tasks/{task.pk}/collect-cash/', HTTP_AUTHORIZATION='Bearer courier').status_code
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            codes = list(pool.map(lambda _: collect(), range(2)))
        self.assertEqual(codes, [200, 200])
        self.assertEqual(Payment.objects.filter(order=fixture.order, provider='cash').count(), 1)
