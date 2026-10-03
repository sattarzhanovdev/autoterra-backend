import json
from datetime import timedelta
from io import StringIO
from unittest.mock import patch
from django.contrib.auth.models import User
from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from .models import AuthToken, ClientProfile, Distributor, Notification, Order, Product, Region, Store
from .services.stock import restore_order_stock


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
                   ORDER_NOTIFICATION_EMAILS=['orders@example.com'],
                   REGISTRATION_NOTIFICATION_EMAILS=['registrations@example.com'])
class ProductionFlowTests(TestCase):
    def setUp(self):
        self.manager = self.user('manager', 'manager')
        self.dealer = self.user('dealer', 'distributor')
        self.admin = self.user('admin', 'admin')
        self.dist = Distributor.objects.create(user=self.dealer, name='A', inn='1111111111')
        self.other = Distributor.objects.create(name='B', inn='2222222222')
        self.region = Region.objects.create(code='A', name='A', distributor=self.dist, manager=self.manager)
        self.region_b = Region.objects.create(code='B', name='B', distributor=self.other)
        self.product = Product.objects.create(distributor=self.dist, sku='P', name='Paint', category='Paint', brand='Brand', price=100, quantity=10)
        self.foreign = Product.objects.create(distributor=self.other, sku='P', name='Other', price=200, quantity=10)
        self.payload = dict(username='79000000000', password='password123', email='client@example.com',
                            termsAccepted=True, personalDataConsent=True, inn='1234567890', region_id=self.region.pk,
                            company_name='СТО', contact_name='Иван', store_address='Адрес')
        self.response = self.client.post('/api/auth/register/', self.payload, content_type='application/json')
        self.assertEqual(self.response.status_code, 201, self.response.content)
        self.customer = ClientProfile.objects.get(user__username=self.payload['username'])
        self.token = self.response.json()['token']

    def user(self, name, role):
        user = User.objects.create_user(name, password='password123')
        user.profile.role = role
        user.profile.save()
        AuthToken.objects.create(user=user, key=name)
        return user

    def post(self, path, body=None, token=None):
        return self.client.post(path, body or {}, content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {token or self.token}')

    def activate(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.post(f'/api/manager/clients/{self.customer.pk}/status/', {'status': 'active'}, 'manager')
        self.assertEqual(response.status_code, 200, response.content)

    def order(self, qty=7, product=None):
        return self.post('/api/orders/create/', {'storeId': self.customer.stores.first().pk, 'items': [{'productId': (product or self.product).pk, 'quantity': qty}]})

    def test_moderation_and_region(self):
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(self.customer.user.email, 'client@example.com')
        self.assertEqual(self.customer.manager, self.manager)
        self.assertEqual(self.customer.distributor, self.dist)
        self.assertTrue(self.response.json()['requires_approval'])
        for state in ('new', 'under_review', 'blocked', 'archived'):
            ClientProfile.objects.filter(pk=self.customer.pk).update(status=state)
            response = self.client.get('/api/products/', HTTP_AUTHORIZATION=f'Bearer {self.token}')
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()['code'], 'account_not_active')
            self.assertEqual(self.order().status_code, 403)
        self.activate()
        self.assertTrue(Notification.objects.filter(user=self.customer.user, body__contains='подтвержден').exists())
        response = self.client.get('/api/products/', HTTP_AUTHORIZATION=f'Bearer {self.token}')
        self.assertEqual([p['id'] for p in response.json()['results']], [str(self.product.pk)])
        self.assertEqual(self.order(product=self.foreign).status_code, 400)

    def test_reservation_cancel_idempotency_and_validation(self):
        self.activate()
        for qty in (0, -1, 'bad', 1.5, True):
            self.assertEqual(self.order(qty).status_code, 400)
        response = self.order()
        self.assertEqual(response.status_code, 201, response.content)
        order = Order.objects.get(pk=response.json()['order']['id'])
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, 3)
        self.assertEqual(self.order(5).status_code, 400)
        self.assertEqual(self.post(f'/api/orders/{order.pk}/confirm/', token='dealer').status_code, 200)
        self.assertEqual(self.post(f'/api/orders/{order.pk}/cancel/').status_code, 200)
        restore_order_stock(order)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, 10)

    def test_backorder_does_not_invent_stock(self):
        self.activate()
        self.product.status, self.product.quantity = 'onOrder', 2
        self.product.save()
        order = Order.objects.get(pk=self.order(7).json()['order']['id'])
        self.assertEqual(order.items.get().reserved_quantity, 2)
        self.post(f'/api/orders/{order.pk}/cancel/')
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, 2)
        self.assertEqual(self.product.status, 'onOrder')

    def test_adjustment_requires_client_acceptance(self):
        self.activate()
        order = Order.objects.get(pk=self.order(10).json()['order']['id'])
        response = self.post(f'/api/orders/{order.pk}/adjust/', {'items': [{'itemId': order.items.get().pk, 'quantity': 6}], 'reason': 'Замена'}, 'dealer')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.post(f'/api/orders/{order.pk}/pay/').status_code, 400)
        self.assertEqual(self.post(f'/api/orders/{order.pk}/confirm/', token='dealer').status_code, 400)
        self.assertEqual(self.post(f'/api/orders/{order.pk}/accept-adjustment/').status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, 'confirmed')
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, 4)

    def test_expiry_and_pending_payment_guard(self):
        self.activate()
        order = Order.objects.get(pk=self.order().json()['order']['id'])
        Order.objects.filter(pk=order.pk).update(created_at=timezone.now()-timedelta(days=2))
        call_command('expire_unpaid_orders', stdout=StringIO())
        call_command('expire_unpaid_orders', stdout=StringIO())
        order.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(order.status, 'cancelled')
        self.assertEqual(self.product.quantity, 10)

    def test_duplicate_inn_activation_persists(self):
        self.payload.update(username='79000000001', region_id=self.region_b.pk)
        response = self.client.post('/api/auth/register/', self.payload, content_type='application/json')
        self.assertEqual(response.status_code, 201, response.content)
        branch = ClientProfile.objects.get(user__username='79000000001')
        self.assertEqual(branch.status, 'under_review')
        branch.status = 'active'
        branch.save()
        branch.refresh_from_db()
        self.assertEqual(branch.status, 'active')

    def test_admin_role_and_manager_order_isolation(self):
        self.activate()
        order = Order.objects.get(pk=self.order().json()['order']['id'])
        self.assertEqual(self.client.get('/api/distributor/orders/', HTTP_AUTHORIZATION='Bearer admin').status_code, 200)
        self.region.manager = None
        self.region.save()
        self.assertEqual(self.client.get(f'/api/orders/{order.pk}/', HTTP_AUTHORIZATION='Bearer manager').status_code, 403)

    @patch('django.core.mail.EmailMultiAlternatives.send', side_effect=RuntimeError('smtp offline'))
    def test_smtp_failure_does_not_rollback_order(self, send):
        self.activate()
        self.assertEqual(self.order().status_code, 201)
        self.assertEqual(Order.objects.count(), 1)

    @override_settings(YOOKASSA_SHOP_ID='shop-test', YOOKASSA_SECRET_KEY='test-only')
    def test_full_payment_delivery_and_turnover(self):
        from .models import Payment, client_turnover, PartnerTier
        from .services import payments
        PartnerTier.objects.update_or_create(name='Silver', defaults={'threshold': 500})
        self.activate()
        order = Order.objects.get(pk=self.order(7).json()['order']['id'])
        self.assertEqual(self.post(f'/api/orders/{order.pk}/pay/').status_code, 400)
        self.assertEqual(self.post(f'/api/orders/{order.pk}/confirm/', token='admin').status_code, 200)
        provider = {'id': 'provider-1', 'status': 'pending', 'paid': False,
                    'amount': {'value': '700.00', 'currency': 'RUB'},
                    'metadata': {'order_id': str(order.pk)}, 'recipient': {'account_id': 'shop-test'},
                    'confirmation': {'confirmation_url': 'https://yoomoney.ru/pay/test'}}
        with patch.object(payments, 'create_payment', return_value=provider), patch.object(payments, 'fetch_payment', return_value=provider):
            first = self.post(f'/api/orders/{order.pk}/pay/')
            second = self.post(f'/api/orders/{order.pk}/pay/')
        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(second.status_code, 201, second.content)
        self.assertEqual(Payment.objects.filter(order=order).count(), 1)
        self.assertEqual(first.json()['payment']['id'], second.json()['payment']['id'])
        provider.update(status='succeeded', paid=True)
        with patch.object(payments, 'fetch_payment', return_value=provider):
            for _ in range(2):
                response = self.client.post('/api/payments/yookassa/webhook/', {'event': 'payment.succeeded', 'object': {'id': 'provider-1'}}, content_type='application/json')
                self.assertEqual(response.status_code, 200, response.content)
        order.refresh_from_db()
        self.assertEqual(order.status, 'paid')
        self.assertEqual(self.post(f'/api/orders/{order.pk}/pay/').status_code, 400)
        self.assertEqual(self.post(f'/api/orders/{order.pk}/ship/', token='dealer').status_code, 200)
        response = self.post(f'/api/distributor/orders/{order.pk}/status/', {'status': 'fulfilled'}, 'dealer')
        self.assertEqual(response.status_code, 200, response.content)
        self.customer.refresh_from_db()
        self.assertEqual(client_turnover(self.customer), 700)
        self.assertEqual(self.customer.partner_status, 'Silver')

    def test_expiry_never_cancels_pending_or_paid_orders(self):
        from .models import Payment
        self.activate()
        order = Order.objects.get(pk=self.order(2).json()['order']['id'])
        Order.objects.filter(pk=order.pk).update(created_at=timezone.now()-timedelta(days=2), status='confirmed')
        payment = Payment.objects.create(order=order, amount=200, status='pending')
        call_command('expire_unpaid_orders', stdout=StringIO())
        order.refresh_from_db()
        self.assertEqual(order.status, 'confirmed')
        payment.status = 'succeeded'
        payment.save()
        Order.objects.filter(pk=order.pk).update(status='paid')
        call_command('expire_unpaid_orders', stdout=StringIO())
        order.refresh_from_db()
        self.assertEqual(order.status, 'paid')
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, 8)

    def test_distributor_cannot_read_other_scope(self):
        self.activate()
        self.other.user = self.user('other-dealer', 'distributor')
        self.other.save()
        order = Order.objects.get(pk=self.order(2).json()['order']['id'])
        for path in ('orders', 'clients', 'stock'):
            response = self.client.get(f'/api/distributor/{path}/', HTTP_AUTHORIZATION='Bearer other-dealer')
            self.assertEqual(response.status_code, 200, response.content)
            ids = [item['id'] for item in response.json()['results']]
            own_id = {'orders': order.pk, 'clients': self.customer.pk, 'stock': self.product.pk}[path]
            self.assertNotIn(str(own_id), ids)
        self.assertEqual(self.post(f'/api/orders/{order.pk}/confirm/', token='other-dealer').status_code, 404)
        response = self.client.get(f'/api/distributor/stock/?clientId={self.customer.pk}', HTTP_AUTHORIZATION='Bearer other-dealer')
        self.assertEqual(response.status_code, 404)

    def test_password_reset_requires_email_challenge(self):
        payload = {'phone': self.payload['username'], 'inn': self.payload['inn'], 'new_password': 'NewPassword123'}
        result = self.client.post('/api/auth/password-reset/', payload, content_type='application/json')
        self.assertEqual(result.json()['status'], 'code_sent')
        self.customer.user.refresh_from_db()
        self.assertTrue(self.customer.user.check_password('password123'))
        code = mail.outbox[-1].body.strip().splitlines()[-1]
        result = self.client.post('/api/auth/password-reset/', {**payload, 'code': code}, content_type='application/json')
        self.assertEqual(result.status_code, 200, result.content)
        self.customer.user.refresh_from_db()
        self.assertTrue(self.customer.user.check_password('NewPassword123'))
        self.assertFalse(AuthToken.objects.filter(user=self.customer.user).exists())
        result = self.client.post('/api/auth/password-reset/', {**payload, 'code': code}, content_type='application/json')
        self.assertEqual(result.status_code, 400)

    def test_verified_purchase_automatically_upgrades_tier(self):
        from .models import Purchase, PartnerTier
        PartnerTier.objects.update_or_create(name='Silver', defaults={'threshold': 500})
        self.activate()
        purchase = Purchase.objects.create(client=self.customer, distributor=self.dist, document_number='INV-1', date='2026-10-01', total_amount=700)
        purchase.status = 'verified'
        purchase.save(update_fields=['status'])
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.partner_status, 'Silver')
