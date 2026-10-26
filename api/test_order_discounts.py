from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase

from .models import ClientBrandDiscount, ClientPriceOverride, ClientProfile, Order, PartnerTier, RankDiscount
from .services.order_discounts import order_discount_details, quote_order
from .services.pricing import apply_discount, price_details
from .services.orders import adjust_order, OrderError
from .test_client_discount import _ClientDiscountBase


class LadderTests(SimpleTestCase):
    def test_boundaries_and_non_accumulating_steps(self):
        for subtotal, expected in [('0', '0'), ('6999', '0'), ('9999.99', '0'),
                                   ('10000', '3'), ('14999', '3'), ('15000', '3.5'),
                                   ('19999', '3.5'), ('20000', '4'), ('25000', '4.5'), ('30000', '5')]:
            with self.subTest(subtotal=subtotal):
                self.assertEqual(order_discount_details(subtotal)['orderDiscountPercent'], Decimal(expected))

    def test_next_threshold_and_progress(self):
        for subtotal, current, nxt, threshold, remaining, progress in [
            ('7000', '0', '3', '10000', '3000', '0.7'),
            ('10000', '3', '3.5', '15000', '5000', '0'),
            ('12000', '3', '3.5', '15000', '3000', '0.4'),
            ('12500', '3', '3.5', '15000', '2500', '0.5'),
            ('15000', '3.5', '4', '20000', '5000', '0')]:
            result = order_discount_details(subtotal)
            self.assertEqual([result[key] for key in ('orderDiscountPercent', 'nextDiscountPercent',
                'nextThreshold', 'amountToNextThreshold', 'progress')], list(map(Decimal, [current, nxt, threshold, remaining, progress])))

    def test_cap_even_for_dirty_percent_and_rounding(self):
        for base in ['0.01', '0.02', '0.03', '0.04', '1.01', '999.99', '1000']:
            price = apply_discount(base, 100)
            self.assertGreaterEqual(price, Decimal(base) * Decimal('0.6'))

    def test_dirty_personal_data_capped_without_database(self):
        client = SimpleNamespace(personal_discount_percent=Decimal('100'), distributor_id=1,
                                 _personal_prices={}, _brand_discounts={})
        product = SimpleNamespace(pk=1, price=Decimal('1000'), distributor_id=1, brand='A')
        result = price_details(client, product)
        self.assertEqual(result['price'], 600)
        self.assertEqual(result['effective_discount_percent'], 40)
        client._personal_prices = {1: Decimal('100')}
        self.assertEqual(price_details(client, product)['price'], 600)

    def test_dirty_brand_and_rank_data_are_capped(self):
        product = SimpleNamespace(pk=1, price=Decimal('1000'), distributor_id=1, brand='A', category='')
        client = SimpleNamespace(personal_discount_percent=None, distributor_id=1,
            _personal_prices={}, _brand_discounts={'a': Decimal('100')}, partner_status='Platinum')
        self.assertEqual(price_details(client, product)['price'], 600)
        client._brand_discounts = {}
        rule = SimpleNamespace(percent=Decimal('100'), tier=SimpleNamespace(name='Platinum'),
                               distributor_id=None, product_category='')
        client._rank_discount_cache = {1: [rule]}
        self.assertEqual(price_details(client, product)['price'], 600)


class OrderDiscountTests(_ClientDiscountBase):
    def quote(self, quantity, **extra):
        return self.http.post('/api/orders/quote/', {'items': [
            {'productId': str(self.product.pk), 'quantity': quantity}], **extra},
            content_type='application/json', **self.auth(self.token))

    def create(self, quantity):
        with patch('api.views._notify_operator_order'), patch('api.views._send_order_email'):
            return self.http.post('/api/orders/create/', {'items': [
                {'productId': str(self.product.pk), 'quantity': quantity, 'price': 1}],
                'discountPercent': 100, 'total': 1}, content_type='application/json', **self.auth(self.token))

    def test_quotes_match_saved_orders_and_ignore_injected_prices(self):
        self.set_discount('0')
        for quantity, percent, total in [(7, '0', 7000), (10, '3', 9700), (15, '3.5', 14475)]:
            with self.subTest(quantity=quantity):
                quote = self.quote(quantity, discountPercent=100, total=1)
                self.assertEqual(quote.status_code, 200)
                summary = quote.json()['orderDiscount']
                self.assertEqual(Decimal(summary['orderDiscountPercent']), Decimal(percent))
                self.assertEqual(Decimal(summary['total']), total)
                result = self.create(quantity)
                self.assertEqual(result.status_code, 201, result.content)
                order = Order.objects.get(pk=result.json()['order']['id'])
                self.assertEqual(order.total_amount, total)
                self.assertEqual(order.items.get().price, Decimal(quote.json()['items'][0]['price']))

    def test_personal_discount_adds_percentage_points_from_base(self):
        self.set_discount('22')
        result = self.quote(15).json()['orderDiscount']
        self.assertEqual(Decimal(result['subtotalBeforeOrderDiscount']), 11700)
        self.assertEqual(Decimal(result['discountAmount']), 450)
        self.assertEqual(Decimal(result['total']), 11250)
        row = self.http.get('/api/products/', **self.auth(self.token)).json()['results'][0]
        self.assertEqual((row['price'], row['basePrice'], row['effectiveDiscountPercent']), (780, 1000, 22))

    def test_effective_cap_and_maximum_state(self):
        for existing, quantity in [('39', 20), ('40', 134)]:
            self.set_discount(existing)
            result = self.quote(quantity).json()
            self.assertEqual(Decimal(result['items'][0]['price']), 600)
            summary = result['orderDiscount']
            self.assertTrue(summary['maximumReached'])
            self.assertIsNone(summary['nextThreshold'])
            self.assertIsNone(summary['nextDiscountPercent'])

    def test_contract_price_not_discounted_again_and_dirty_price_capped(self):
        override = ClientPriceOverride.objects.create(client=self.client_profile, product=self.product, price=700)
        result = self.quote(20).json()
        self.assertEqual(Decimal(result['items'][0]['price']), 700)
        self.assertEqual(Decimal(result['orderDiscount']['discountAmount']), 0)
        self.assertFalse(result['orderDiscount']['hasEligibleItems'])
        ClientPriceOverride.objects.filter(pk=override.pk).update(price=100)
        self.assertEqual(Decimal(self.quote(20).json()['items'][0]['price']), 600)

    def test_override_api_and_model_reject_above_cap(self):
        url = f'/api/clients/{self.client_profile.pk}/prices/{self.product.pk}/'
        result = self.http.patch(url, {'price': '599.99'}, content_type='application/json', **self.auth('admin'))
        self.assertEqual(result.status_code, 400, result.content)
        with self.assertRaises(ValidationError):
            ClientPriceOverride.objects.create(client=self.client_profile, product=self.product, price=599)

    def test_quantity_change_reprices_all_items_both_directions(self):
        self.set_discount('0')
        created = self.create(12)
        order = Order.objects.get(pk=created.json()['order']['id'])
        item = order.items.get()
        for quantity, expected in [(15, 965), (9, 1000)]:
            adjust_order(order.pk, {'items': [{'itemId': item.pk, 'quantity': quantity}]})
            item.refresh_from_db()
            self.assertEqual(item.price, expected)
            self.assertEqual(Order.objects.get(pk=order.pk).total_amount, quantity * expected)

    def test_paid_orders_cannot_be_adjusted_or_replaced(self):
        order = Order.objects.get(pk=self.create(12).json()['order']['id'])
        order.status = 'paid'
        order.save()
        with self.assertRaises(OrderError):
            adjust_order(order.pk, {'items': [{'itemId': order.items.get().pk, 'quantity': 15}]})
        result = self.http.post(f'/api/orders/{order.pk}/replace/', {'items': [
            {'productId': self.product.pk, 'quantity': 15}]}, content_type='application/json', **self.auth(self.token))
        self.assertEqual(result.status_code, 400)

    def test_quote_auth_scope_and_validation(self):
        self.assertEqual(self.http.post('/api/orders/quote/', {}, content_type='application/json').status_code, 401)
        for quantity in [0, -1, True, '1.5', [], None, 1000001]:
            self.assertEqual(self.quote(quantity).status_code, 400)
        self.product.is_active = False
        self.product.save()
        self.assertEqual(self.quote(10).status_code, 400)

    def test_all_percentage_fields_reject_above_forty(self):
        self.assertEqual(self.set_discount('40').status_code, 200)
        for value in ['40.01', '100']:
            self.assertEqual(self.set_discount(value).status_code, 400)
            with self.assertRaises(ValidationError):
                ClientBrandDiscount(client=self.client_profile, brand='a', percent=value).full_clean()
            with self.assertRaises(ValidationError):
                RankDiscount(tier=PartnerTier.objects.get(name='Platinum'), percent=value).full_clean()
            for model, field in [(ClientProfile, 'personal_discount_percent'), (RankDiscount, 'percent')]:
                with self.assertRaises(IntegrityError), transaction.atomic():
                    model.objects.all().update(**{field: value})

    def test_mixed_basket_discount_and_contract_are_separate(self):
        from .models import Product
        other = Product.objects.create(distributor=self.distributor, sku='fixed', price=1000, quantity=100)
        ClientPriceOverride.objects.create(client=self.client_profile, product=other, price=700)
        self.set_discount('22')
        prices, result = quote_order(self.fresh(), {self.product.pk: self.product, other.pk: other},
                                      {self.product.pk: 10, other.pk: 10})
        self.assertEqual(result['subtotalBeforeOrderDiscount'], 14800)
        self.assertEqual(prices[self.product.pk], 750)
        self.assertEqual(prices[other.pk], 700)
        self.assertEqual(result['total'], 14500)
