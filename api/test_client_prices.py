from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext

from .models import AuthToken, ClientPriceOverride, Distributor, Product, PartnerTier, RankDiscount, Order, AuditLog
from .services.pricing import price_for_client, price_details
from .test_bonus_account import _Base


class ClientPriceTests(_Base):
    def setUp(self):
        super().setUp()
        self.manager = self.staff('manager', 'manager')
        self.admin = self.staff('admin', 'admin')
        self.dist = self.staff('distributor', 'distributor')
        self.distributor.user = self.dist
        self.distributor.save()
        self.region.manager = self.manager
        self.region.save()
        self.client_profile.partner_status = 'Platinum'
        self.client_profile.save()
        RankDiscount.objects.create(tier=PartnerTier.objects.get(name='Platinum'), percent=20)
        self.list_url = f'/api/clients/{self.client_profile.pk}/prices/'
        self.url = f'{self.list_url}{self.product.pk}/'

    def staff(self, username, role):
        user = User.objects.create_user(username=username)
        user.profile.role = role
        user.profile.save()
        AuthToken.objects.create(user=user, key=username)
        return user

    def auth(self, key='manager'):
        return {'HTTP_AUTHORIZATION': f'Bearer {key}'}

    def save_price(self, price='650.25', key='manager', **extra):
        return self.http.patch(self.url, {'price': price, **extra}, content_type='application/json', **self.auth(key))

    def fresh_client(self):
        return type(self.client_profile).objects.get(pk=self.client_profile.pk)

    def test_override_precedes_rank_and_can_exceed_base(self):
        for price in ('650.25', '1200.00'):
            self.assertIn(self.save_price(price).status_code, (200, 201))
            self.assertEqual(price_for_client(self.fresh_client(), self.product), Decimal(price))
        self.assertEqual(ClientPriceOverride.objects.count(), 1)
        self.assertEqual(RankDiscount.objects.get().percent, 20)

    def test_disabled_and_deleted_override_fall_back_to_rank(self):
        self.save_price()
        result = self.http.patch(self.url, {'isActive': False}, content_type='application/json', **self.auth())
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['item']['price'], '800.00')
        self.http.delete(self.url, **self.auth())
        self.assertEqual(price_for_client(self.fresh_client(), self.product), Decimal('800'))
        self.assertFalse(ClientPriceOverride.objects.exists())

    def test_no_rank_falls_back_to_base(self):
        RankDiscount.objects.all().delete()
        self.assertEqual(price_for_client(self.fresh_client(), self.product), Decimal('1000'))
        self.assertEqual(price_for_client(None, self.product), Decimal('1000'))

    def test_override_is_private_to_client_and_product(self):
        self.save_price()
        other = self._client('other-client', '2223334445')
        self.assertEqual(price_for_client(other, self.product), Decimal('1000'))
        other_product = Product.objects.create(distributor=self.distributor, sku='other', price=1000)
        self.assertEqual(price_for_client(self.fresh_client(), other_product), Decimal('800'))

    def test_catalog_and_order_use_override_and_order_snapshot_is_preserved(self):
        self.save_price()
        response = self.http.get('/api/products/', **self.auth(self.token))
        product = response.json()['results'][0]
        self.assertEqual(product['price'], 650.25)
        self.assertEqual(product['personalPrice'], 650.25)
        self.assertEqual(product['basePrice'], 1000)
        self.assertEqual(product['priceSource'], 'personal')
        self.assertEqual(product['discountPercent'], 0)
        response = self.http.post('/api/orders/create/', {'items': [
            {'productId': self.product.pk, 'quantity': 2, 'price': '0.01'}]},
            content_type='application/json', **self.auth(self.token))
        self.assertEqual(response.status_code, 201, response.content)
        order = Order.objects.get(pk=response.json()['order']['id'])
        self.assertEqual(order.total_amount, Decimal('1300.50'))
        self.save_price('700')
        self.http.delete(self.url, **self.auth())
        self.assertEqual(order.items.get().price, Decimal('650.25'))

    def test_staff_search_exposes_both_prices_and_inactive_records(self):
        self.save_price(isActive=False)
        result = self.http.get(self.list_url, {'search': 'P-1'}, **self.auth())
        item = result.json()['results'][0]
        self.assertTrue(result.json()['canEdit'])
        self.assertEqual(item['basePrice'], '1000.00')
        self.assertEqual(item['personalPrice'], '650.25')
        self.assertEqual(item['rankPrice'], '800.00')
        self.assertEqual(item['price'], '800.00')
        self.assertFalse(item['isActive'])
        self.assertIsNotNone(item['updatedAt'])

    def test_catalog_search_to_create_and_pagination(self):
        result = self.http.get(self.list_url, {'overridesOnly': 'false', 'search': 'Краска', 'pageSize': 1}, **self.auth())
        self.assertEqual(result.json()['count'], 1)
        self.assertIsNone(result.json()['results'][0]['personalPrice'])
        self.assertEqual(self.http.get(self.list_url, **self.auth()).json()['count'], 0)

    def test_anonymous_client_courier_and_expert_denied(self):
        self.assertEqual(self.http.get(self.list_url).status_code, 401)
        for key in [self.token, self.staff('courier', 'courier').username, self.staff('expert', 'ai_expert').username]:
            self.assertEqual(self.http.get(self.list_url, **self.auth(key)).status_code, 403)
            self.assertEqual(self.save_price(key=key).status_code, 403)
            self.assertEqual(self.http.delete(self.url, **self.auth(key)).status_code, 403)

    def test_distributor_can_read_but_cannot_write(self):
        self.save_price()
        response = self.http.get(self.list_url, **self.auth('distributor'))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['canEdit'])
        self.assertEqual(response.json()['results'][0]['personalPrice'], '650.25')
        self.assertEqual(self.save_price(key='distributor').status_code, 403)
        self.assertEqual(self.http.delete(self.url, **self.auth('distributor')).status_code, 403)
        stock = self.http.get('/api/distributor/stock/', {'clientId': self.client_profile.pk}, **self.auth('distributor'))
        self.assertEqual(stock.json()['results'][0]['price'], 650.25)

    def test_manager_outside_region_and_other_distributor_cannot_read_or_write(self):
        other = self.staff('other-manager', 'manager')
        for key in [other.username, 'other-dist']:
            if key == 'other-dist':
                user = self.staff(key, 'distributor')
                Distributor.objects.create(user=user, name='Other', inn='4444444444')
            self.assertEqual(self.http.get(self.list_url, **self.auth(key)).status_code, 404)
            self.assertIn(self.save_price(key=key).status_code, (403, 404))

    def test_admin_can_edit_unmanaged_client_and_put_is_upsert(self):
        self.region.manager = None
        self.region.save()
        for _ in range(2):
            response = self.http.put(self.url, {'price': '400.00'}, content_type='application/json', **self.auth('admin'))
            self.assertIn(response.status_code, (200, 201))
        self.assertEqual(ClientPriceOverride.objects.count(), 1)
        self.assertEqual(AuditLog.objects.filter(model_name='ClientPriceOverride').count(), 2)

    def test_foreign_distributor_product_cannot_be_assigned(self):
        other = Distributor.objects.create(name='Other', inn='4444444444')
        product = Product.objects.create(distributor=other, sku='foreign', price=100)
        response = self.http.patch(f'{self.list_url}{product.pk}/', {'price': '1'}, content_type='application/json', **self.auth('admin'))
        self.assertEqual(response.status_code, 404)
        with self.assertRaises(ValidationError):
            ClientPriceOverride(client=self.client_profile, product=product, price=10).full_clean()

    def test_invalid_prices_do_not_modify_existing_price(self):
        self.save_price()
        for price in [None, True, {}, [], '', '-1', '0', 'NaN', 'Infinity', '1.001', '10000000000', '1e10000']:
            with self.subTest(price=price):
                self.assertEqual(self.save_price(price).status_code, 400)
        self.assertEqual(self.save_price('1', isActive='false').status_code, 400)
        self.assertEqual(ClientPriceOverride.objects.get().price, Decimal('650.25'))

    def test_database_prevents_duplicate_or_nonpositive_override(self):
        self.save_price()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ClientPriceOverride.objects.create(client=self.client_profile, product=self.product, price=10)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ClientPriceOverride.objects.update(price=0)

    def test_override_queries_do_not_grow_with_catalog_size(self):
        self.save_price()
        def count():
            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(self.http.get('/api/products/?pageSize=100', **self.auth(self.token)).status_code, 200)
            return len(ctx)
        before = count()
        for n in range(8):
            product = Product.objects.create(distributor=self.distributor, sku=f'P-{n+2}', price=100)
            ClientPriceOverride.objects.create(client=self.client_profile, product=product, price=50)
        self.assertEqual(count(), before)

    def test_moved_client_does_not_use_previous_distributor_override(self):
        self.save_price()
        other = Distributor.objects.create(name='Other', inn='4444444444')
        client = self.fresh_client()
        client.distributor = other
        self.assertIsNone(price_details(client, self.product)['personal_price'])

    def test_central_admin_client_form_contains_price_inline(self):
        admin = User.objects.create_superuser('django-admin', password='test-password')
        self.http.force_login(admin)
        response = self.http.get(f'/admin/api/clientprofile/{self.client_profile.pk}/change/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Персональные цены')

    def test_adjustment_new_items_use_personal_price(self):
        self.save_price('650.25')
        other = Product.objects.create(distributor=self.distributor, sku='original', price=1000, quantity=10)
        order = self._order(1000, status='new')
        order.items.update(product=other)
        response = self.http.post(f'/api/orders/{order.pk}/adjust/', {
            'newItems': [{'productId': self.product.pk, 'quantity': 2}],
        }, content_type='application/json', **self.auth('admin'))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(order.items.get(product=self.product).price, Decimal('650.25'))

    def test_price_list_pagination_does_not_hide_other_pages(self):
        self.save_price()
        for n in range(3):
            product = Product.objects.create(distributor=self.distributor, sku=f'NEW-{n}', name=f'Товар {n}', price=100)
            ClientPriceOverride.objects.create(client=self.client_profile, product=product, price=50)
        seen = []
        for page in (1, 2):
            response = self.http.get(self.list_url, {'page': page, 'pageSize': 2}, **self.auth())
            self.assertEqual(response.json()['count'], 4)
            seen.extend(row['productId'] for row in response.json()['results'])
        self.assertEqual(len(set(seen)), 4)
