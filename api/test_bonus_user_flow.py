from datetime import date
from decimal import Decimal
from unittest.mock import patch
from django.test import override_settings
from .test_bonus_account import _Base
from .models import ClientProfile, Purchase, Referral, Order, BonusTransaction
from .services import bonuses, payments


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend', YOOKASSA_SHOP_ID='test-only', YOOKASSA_SECRET_KEY='test-only', YOOKASSA_RECEIPT_EMAIL='receipt@example.test')
class BonusUserFlowTests(_Base):
    def test_registration_referral_purchase_checkout_payment_retry_cancel_history(self):
        inviter = self.client_profile
        registered = self.http.post('/api/auth/register/', {
            'username': '+79002223344', 'password': 'verysecret123',
            'email': 'invitee@example.test', 'termsAccepted': True, 'personalDataConsent': True,
            'inn': '7778889990', 'region_id': str(self.region.pk), 'company_name': 'ООО Приглашённый',
            'contact_name': 'Пётр', 'store_address': 'Москва, Ленина 1',
            'referralCode': '  ' + inviter.referral_code.lower() + '  ',
        }, content_type='application/json')
        self.assertEqual(registered.status_code, 201, registered.content)
        invited = ClientProfile.objects.get(inn='7778889990')
        invited.status = 'active'  # Existing required admin approval.
        invited.save(update_fields=['status'])
        logged_in = self.http.post('/api/login/', {'phone': '+79002223344', 'password': 'verysecret123'}, content_type='application/json')
        self.assertEqual(logged_in.status_code, 200, logged_in.content)
        referral = Referral.objects.get(inviter=inviter, invitee_inn=invited.inn)
        self.assertTrue(referral.counts_toward_bonus)
        Purchase.objects.create(client=invited, distributor=self.distributor, document_number='FLOW-1', date=date.today(), total_amount=Decimal(300000), status='verified')
        self.assertEqual(bonuses.balance(inviter), Decimal(4500))
        auth = {'HTTP_AUTHORIZATION': f'Bearer {self.token}'}
        data = self.http.get('/api/referrals/', **auth).json()
        self.assertEqual(data['results'][0]['bonusRate'], 1.5)
        self.assertEqual(data['results'][0]['purchaseAmount'], 300000)
        self.assertEqual(Decimal(data['stats']['totalEarned']), Decimal(4500))
        self.assertEqual(self.http.get('/api/products/', **auth).status_code, 200)
        basket = {'items': [{'productId': str(self.product.pk), 'quantity': 1}], 'bonusAmount': '9999'}
        quote = self.http.post('/api/orders/quote/', basket, content_type='application/json', **auth).json()
        limit = Decimal(quote['bonus']['maxAllowed'])
        self.assertEqual(limit, Decimal(quote['orderDiscount']['total']) * Decimal('.1'))
        self.assertEqual(bonuses.balance(inviter), Decimal(4500))
        with patch('api.views._notify_operator_order'), patch('api.views._send_order_email'):
            created = self.http.post('/api/orders/create/', basket, content_type='application/json', **auth)
        self.assertEqual(created.status_code, 201, created.content)
        order = Order.objects.get(pk=created.json()['order']['id'])
        self.assertEqual(order.bonus_requested, limit)
        self.assertEqual(bonuses.balance(inviter), Decimal(4500))
        order.status = 'confirmed'  # Distributor confirms order.
        order.save(update_fields=['status'])
        response = {
            'id': 'flow-payment', 'status': 'pending', 'paid': False,
            'amount': {'value': str(Decimal(order.total_amount) - limit), 'currency': 'RUB'},
            'metadata': {'order_id': str(order.pk)}, 'recipient': {'account_id': 'test-only'},
            'confirmation': {'confirmation_url': 'https://yoomoney.ru/pay/test'},
        }
        with patch.object(payments, 'create_payment', return_value=response), patch.object(payments, 'fetch_payment', return_value=response):
            for _ in range(2):
                paid = self.http.post(f'/api/orders/{order.pk}/pay/', {}, content_type='application/json', **auth)
                self.assertEqual(paid.status_code, 201, paid.content)
        self.assertEqual(BonusTransaction.objects.filter(order=order, kind='order').count(), 1)
        self.assertEqual(bonuses.balance(inviter), Decimal(4500) - limit)
        response['status'] = 'canceled'
        with patch.object(payments, 'fetch_payment', return_value=response):
            for _ in range(2):
                canceled = self.http.post('/api/payments/yookassa/webhook/', {'event': 'payment.canceled', 'object': {'id': 'flow-payment'}}, content_type='application/json')
                self.assertEqual(canceled.status_code, 200, canceled.content)
        self.assertEqual(bonuses.balance(inviter), Decimal(4500))
        cancel = self.http.post(f'/api/orders/{order.pk}/cancel/', {}, content_type='application/json', **auth)
        self.assertEqual(cancel.status_code, 200, cancel.content)
        history = self.http.get('/api/bonus-account/', **auth).json()['results']
        self.assertEqual({row['kind'] for row in history}, {'referral', 'order', 'refund'})
