from .test_bonus_account import _Base
from .models import Order, AuthToken, Payment


class OrderReplacementTests(_Base):
    def post(self, path, payload, token=None):
        return self.http.post(path, payload, content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {token or self.token}')

    def create(self):
        response = self.post('/api/orders/create/', {'items': [{'productId': self.product.pk, 'quantity': 2}]})
        self.assertEqual(response.status_code, 201, response.content)
        return Order.objects.get(pk=response.json()['order']['id'])

    def test_failed_replacement_preserves_original_and_reserved_stock(self):
        order = self.create()
        for items in ([{'productId': self.product.pk, 'quantity': 1001}], [{'productId': 999999, 'quantity': 1}]):
            response = self.post(f'/api/orders/{order.pk}/replace/', {'items': items})
            self.assertEqual(response.status_code, 400, response.content)
            order.refresh_from_db()
            self.product.refresh_from_db()
            self.assertEqual(order.status, 'new')
            self.assertEqual(order.items.get().quantity, 2)
            self.assertEqual(self.product.quantity, 998)
            self.assertEqual(Order.objects.count(), 1)

    def test_success_uses_fresh_discount_and_reserves_only_new_quantity(self):
        order = self.create()
        self.client_profile.personal_discount_percent = 10
        self.client_profile.save()
        response = self.post(f'/api/orders/{order.pk}/replace/', {'items': [{'productId': self.product.pk, 'quantity': 3}]})
        self.assertEqual(response.status_code, 201, response.content)
        order.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(order.status, 'cancelled')
        self.assertEqual(self.product.quantity, 997)
        replacement = Order.objects.get(pk=response.json()['order']['id'])
        self.assertEqual(replacement.total_amount, 2700)

    def test_pending_payment_paid_order_and_other_client_cannot_replace(self):
        order = self.create()
        url = f'/api/orders/{order.pk}/replace/'
        body = {'items': [{'productId': self.product.pk, 'quantity': 1}]}
        Payment.objects.create(order=order, amount=2000, provider='yookassa', status='pending')
        self.assertEqual(self.post(url, body).status_code, 409)
        order.payments.all().delete()
        order.status = 'paid'
        order.save()
        self.assertEqual(self.post(url, body).status_code, 400)
        other = self._client('other', '8888888888')
        AuthToken.objects.create(user=other.user, key='other')
        self.assertEqual(self.post(url, body, 'other').status_code, 404)
