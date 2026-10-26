from decimal import Decimal
from io import BytesIO
from openpyxl import Workbook
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from . import test_client_discount as fixtures
from .models import ClientBrandDiscount, ClientPriceOverride, Product, Order, Distributor, Region, ClientProfile
from .services.pricing import price_details, price_for_client
from .services.product_import import parse_products_workbook, upsert_products


class BrandDiscountTests(fixtures._ClientDiscountBase):
    def setUp(self):
        super().setUp()
        self.product.brand = 'Bosch'
        self.product.save()
        self.set_discount('5')
        self.url = f'/api/clients/{self.client_profile.pk}/brand-discounts/'

    def save_brand(self, percent='10', brand='BOSCH', role='admin'):
        return self.http.patch(self.url, {'discounts': [{'brand': brand, 'percent': percent}]}, content_type='application/json', **self.auth(role))

    def delete_brand(self, brand='bosch', role='admin'):
        return self.http.delete(self.url, {'brand': brand}, content_type='application/json', **self.auth(role))

    def test_brand_over_general_catalog_quote_and_server_order(self):
        self.assertEqual(self.save_brand().status_code, 200)
        catalog = self.http.get('/api/products/', **self.auth(self.token)).json()['results'][0]
        self.assertEqual(catalog['price'], 900)
        self.assertEqual(catalog['discountPercent'], 10)
        self.assertEqual(catalog['priceSource'], 'brand_discount')
        quote = self.http.get(f'/api/products/quote/?ids={self.product.pk}', **self.auth(self.token)).json()['results'][0]
        self.assertEqual(quote['price'], catalog['price'])
        result = self.create_order()
        self.assertEqual(result.status_code, 201, result.content)
        order = Order.objects.get(pk=result.json()['order']['id'])
        self.assertEqual(order.items.get().price, catalog['price'])
        self.assertEqual(order.total_amount, 1800)

    def test_personal_price_wins_in_catalog_and_order(self):
        self.save_brand('80')
        ClientPriceOverride.objects.create(client=self.client_profile, product=self.product, price=777)
        details = price_details(self.fresh(), self.product)
        self.assertEqual(details['price'], 777)
        self.assertEqual(details['price_source'], 'personal')
        result = self.create_order()
        self.assertEqual(result.json()['order']['totalAmount'], 1554)

    def test_delete_restores_general_then_rank(self):
        self.save_brand('20')
        self.assertEqual(self.delete_brand().status_code, 200)
        self.assertEqual(price_for_client(self.fresh(), self.product), 950)
        client = self.fresh()
        client.personal_discount_percent = None
        client.save()
        self.save_brand('30')
        self.delete_brand()
        self.assertEqual(price_details(self.fresh(), self.product)['price_source'], 'rank')
        self.assertEqual(price_for_client(self.fresh(), self.product), 900)

    def test_client_brand_and_distributor_isolation(self):
        self.save_brand('20')
        other_brand = Product.objects.create(distributor=self.distributor, sku='mann', brand='MANN', price=1000)
        self.assertEqual(price_for_client(self.fresh(), other_brand), 950)
        other = self._client('another', '2223334445')
        self.assertEqual(price_for_client(other, self.product), 1000)
        other_distributor = Distributor.objects.create(name='Other', inn='1111111112')
        foreign = Product.objects.create(distributor=other_distributor, sku='bosch', brand='Bosch', price=1000)
        self.assertEqual(price_for_client(self.fresh(), foreign), 950)

    def test_zero_forty_and_fractional_discount(self):
        for percent, price in (('0', 1000), ('40', 600), ('7.50', 925)):
            self.assertEqual(self.save_brand(percent).status_code, 200)
            self.assertEqual(price_for_client(self.fresh(), self.product), price)
        self.assertEqual(ClientBrandDiscount.objects.count(), 1)

    def test_normalization_unique_brand_list_and_query_cache(self):
        Product.objects.create(distributor=self.distributor, sku='second', brand='  BOSCH ', price=1000)
        Product.objects.create(distributor=self.distributor, sku='third', brand='ＢＯＳＣＨ', price=1000)
        self.save_brand('12', brand='  bosch ')
        self.save_brand('10', brand='ＢＯＳＣＨ')
        self.assertEqual(ClientBrandDiscount.objects.count(), 1)
        result = self.http.get(self.url, {'search': ' BOSCH '}, **self.auth('admin')).json()
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['results'][0]['brand'], 'bosch')
        client = self.fresh()
        products = list(Product.objects.filter(distributor=self.distributor))
        with self.assertNumQueries(2):  # Brand rules + product overrides, once per request.
            for product in products:
                self.assertEqual(price_for_client(client, product), 900)
        with self.assertRaises(ValidationError):
            ClientBrandDiscount.objects.create(client=client, brand='BOSCH', percent=10)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ClientBrandDiscount.objects.bulk_create([ClientBrandDiscount(client=client, brand='bosch', percent=1)])

    def test_invalid_batch_is_atomic_and_constraints_enforced(self):
        self.save_brand('10')
        for percent in (-1, 40.01, 100, 101, True, 'NaN', 'Infinity', '1.001', None, [], {}, '1e10000'):
            self.assertEqual(self.save_brand(percent).status_code, 400, percent)
        for body in ([], {}, {'discounts': []}, {'discounts': [{'brand': 'Bosch', 'percent': 5}, {'brand': 'BOSCH', 'percent': 6}]}, {'discounts': [{'brand': 'Bosch', 'percent': 5}, {'brand': 'foreign', 'percent': 6}]}):
            result = self.http.patch(self.url, body, content_type='application/json', **self.auth('admin'))
            self.assertEqual(result.status_code, 400, body)
        self.assertEqual(ClientBrandDiscount.objects.get().percent, 10)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ClientBrandDiscount.objects.filter(client=self.client_profile).update(percent=101)
        with self.assertRaises(ValidationError):
            ClientBrandDiscount.objects.create(client=self.client_profile, brand='mann', percent='1.001')

    def test_roles_foreign_clients_and_foreign_brands(self):
        self.assertEqual(self.save_brand(role='distributor').status_code, 200)
        for role in ('manager', self.token, 'courier', 'ai_expert'):
            self.assertEqual(self.save_brand(role=role).status_code, 403, role)
            self.assertEqual(self.delete_brand(role=role).status_code, 403, role)
        self.assertEqual(self.http.get(self.url, **self.auth('manager')).json()['canEdit'], False)
        self.assertEqual(self.http.get(self.url).status_code, 401)
        other = Distributor.objects.create(name='Other', inn='1111111112')
        Product.objects.create(distributor=other, sku='foreign', brand='Only foreign', price=1000)
        self.assertEqual(self.save_brand(brand='Only foreign').status_code, 400)
        region = Region.objects.create(name='Other', code='other', distributor=other)
        self.client_profile.region = region
        self.client_profile.distributor = other
        self.client_profile.save()
        self.assertEqual(self.save_brand(role='distributor').status_code, 404)
        self.assertEqual(self.delete_brand(role='distributor').status_code, 404)
        self.assertEqual(self.http.get(self.url, **self.auth('distributor')).status_code, 404)
        self.assertEqual(self.save_brand(brand='Only foreign', role='admin').status_code, 200)

    def test_excel_reimport_case_changes_and_temporary_absence(self):
        self.save_brand('10')
        wb = Workbook()
        wb.active.append(['Артикул', 'Название', 'Бренд', 'Цена', 'Остаток'])
        wb.active.append([self.product.sku, self.product.name, '  bOsCh  ', 2000, 100])
        file = BytesIO()
        wb.save(file)
        file.seek(0)
        products, errors = parse_products_workbook(file)
        self.assertTrue(products, errors)
        upsert_products(self.distributor, products)
        upsert_products(self.distributor, products)
        self.product.refresh_from_db()
        self.assertEqual(price_for_client(self.fresh(), self.product), 1800)
        self.assertEqual(ClientBrandDiscount.objects.count(), 1)
        self.product.is_active = False
        self.product.save()
        row = self.http.get(self.url, **self.auth('admin')).json()['results'][0]
        self.assertFalse(row['available'])
        self.assertEqual(row['percent'], '10.00')
        self.assertEqual(self.delete_brand().status_code, 200)

    def test_order_replacement_recalculates_current_brand_price(self):
        result = self.create_order()
        order = Order.objects.get(pk=result.json()['order']['id'])
        self.save_brand('20')
        response = self.http.post(f'/api/orders/{order.pk}/replace/', {'items': [{'productId': self.product.pk, 'quantity': 2}]}, content_type='application/json', **self.auth(self.token))
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()['order']['totalAmount'], 1600)
        self.assertEqual(order.items.get().price, 950)  # Existing snapshot unchanged.

    def test_import_without_brand_column_or_with_empty_brand_preserves_identity(self):
        self.save_brand('10')
        for header, row in ((['Артикул', 'Название', 'Цена'], [self.product.sku, self.product.name, 2000]),
                            (['Артикул', 'Название', 'Бренд', 'Цена'], [self.product.sku, self.product.name, '', 2000])):
            wb = Workbook()
            wb.active.append(header)
            wb.active.append(row)
            file = BytesIO()
            wb.save(file)
            file.seek(0)
            products, errors = parse_products_workbook(file)
            self.assertTrue(products, errors)
            upsert_products(self.distributor, products)
            self.product.refresh_from_db()
            self.assertEqual(self.product.brand, 'Bosch')
            self.assertEqual(price_for_client(self.fresh(), self.product), 1800)
