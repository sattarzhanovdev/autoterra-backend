from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import close_old_connections, connections
from django.test import Client, TransactionTestCase, override_settings, skipUnlessDBFeature

from .models import AuthToken, ClientProfile, Distributor, Order, OrderItem, Payment, Product, Region


@skipUnlessDBFeature('has_select_for_update')
@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
                   ORDER_NOTIFICATION_EMAILS=[], YOOKASSA_SHOP_ID='test-shop',
                   YOOKASSA_SECRET_KEY='test-only')
class ConcurrentOrderTests(TransactionTestCase):
    def setUp(self):
        distributor = Distributor.objects.create(name='A', inn='1234567890')
        region = Region.objects.create(name='A', code='A', distributor=distributor)
        user = User.objects.create_user(username='client')
        self.customer = ClientProfile.objects.create(
            user=user, inn='1234567890', company_name='СТО', region=region,
            distributor=distributor, city='A', contact_name='Иван', phone='1', status='active',
        )
        AuthToken.objects.create(user=user, key='client-token')
        self.product = Product.objects.create(distributor=distributor, sku='P', name='Paint', quantity=10, price=100)

    def race(self, path, bodies):
        barrier = Barrier(len(bodies))

        def request(body):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                response = Client().post(path, body, content_type='application/json',
                                         HTTP_AUTHORIZATION='Bearer client-token')
                return response.status_code, response.json()
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(bodies)) as pool:
            return list(pool.map(request, bodies))

    def test_concurrent_orders_do_not_oversell(self):
        responses = self.race('/api/orders/create/', [
            {'items': [{'productId': self.product.pk, 'quantity': n}]} for n in (7, 5)
        ])
        self.assertEqual(sorted(r[0] for r in responses), [201, 400], responses)
        self.product.refresh_from_db()
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(self.product.quantity + OrderItem.objects.get().reserved_quantity, 10)

    def test_concurrent_payment_reuses_one_attempt(self):
        order = Order.objects.create(client=self.customer, distributor=self.customer.distributor, status='confirmed')
        OrderItem.objects.create(order=order, product=self.product, sku='P', name='Paint', price=100, quantity=2)
        provider = {'id': 'provider-1', 'status': 'pending', 'paid': False,
                    'amount': {'value': '200.00', 'currency': 'RUB'},
                    'metadata': {'order_id': str(order.pk)}, 'recipient': {'account_id': 'test-shop'},
                    'confirmation': {'confirmation_url': 'https://yoomoney.ru/pay/test'}}
        with patch('api.services.payments.create_payment', return_value=provider) as create, patch('api.services.payments.fetch_payment', return_value=provider):
            responses = self.race(f'/api/orders/{order.pk}/pay/', [{}, {}])
        self.assertEqual([r[0] for r in responses], [201, 201], responses)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(len({c.kwargs['idempotence_key'] for c in create.call_args_list}), 1)
        self.assertEqual(responses[0][1]['payment']['id'], responses[1][1]['payment']['id'])
