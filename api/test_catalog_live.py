from decimal import Decimal
from unittest.mock import patch
from .test_bonus_account import _Base
from .models import IntegrationToken, Product, Distributor, SyncLog
from .services.catalog_sync import catalog_revision
from .services.product_import import upsert_products


class CatalogLiveTests(_Base):
    def setUp(self):
        super().setUp()
        self.integration = IntegrationToken.objects.create(distributor=self.distributor, token='catalog-qa')
        self.auth = {'HTTP_AUTHORIZATION': f'Bearer {self.token}'}

    def sync(self, items, **kwargs):
        return self.http.post('/api/integration/erp/catalog-sync/', {'items': items, **kwargs}, content_type='application/json', HTTP_X_INTEGRATION_TOKEN=self.integration.token)

    def catalog(self):
        response = self.http.get('/api/products/', **self.auth)
        self.assertEqual(response['Cache-Control'], 'private, no-store')
        return {row['sku']: row for row in response.json()['results']}

    def test_create_update_all_fields_and_fresh_public_catalog(self):
        self.assertNotIn('TEST-SKU-001', self.catalog())
        before = catalog_revision(self.distributor)
        result = self.sync([{'sku': 'TEST-SKU-001', 'external_id': 'erp-1', 'name': 'Новый товар', 'description': 'Описание', 'brand': 'Новый бренд', 'category': 'Новая категория', 'price': '1000.00', 'quantity': 10, 'images': ['https://example.test/a.jpg'], 'video_url': 'https://example.test/video'}])
        self.assertEqual(result.json()['created'], 1, result.content)
        self.assertNotEqual(catalog_revision(self.distributor), before)
        row = self.catalog()['TEST-SKU-001']
        self.assertEqual((row['price'], row['quantity'], row['brand'], row['category']), (1000, 10, 'Новый бренд', 'Новая категория'))
        self.sync([{'external_id': 'erp-1', 'sku': 'TEST-SKU-001', 'price': '800.00', 'quantity': 3, 'brand': 'Другой бренд', 'category': 'Другая категория', 'status': 'onOrder'}])
        row = self.catalog()['TEST-SKU-001']
        self.assertEqual((row['price'], row['quantity'], row['status']), (800, 3, 'onOrder'))
        self.assertEqual(Product.objects.get(external_id='erp-1').description, 'Описание')
        config = self.http.get('/api/order-config/', **self.auth).json()
        self.assertIn('Другой бренд', config['brands'])
        self.assertIn('Другая категория', config['categories'])
        self.sync([{'sku': 'TEST-SKU-001', 'is_active': False}])
        self.assertNotIn('TEST-SKU-001', self.catalog())

    def test_partial_and_full_snapshot_scoped_deactivation(self):
        other = Distributor.objects.create(name='Другой', inn='1234567890', phone='1', email='a@example.test')
        foreign = Product.objects.create(distributor=other, sku='OTHER', name='Другой товар', category='К', price=1)
        self.sync([{'sku': 'NEW', 'name': 'New'}])
        self.product.refresh_from_db()
        self.assertTrue(self.product.is_active)
        result = self.sync([{'sku': 'NEW', 'brand': 'Changed'}], fullSync=True).json()
        self.assertEqual(result['deactivated'], 1)
        self.product.refresh_from_db(); foreign.refresh_from_db()
        self.assertFalse(self.product.is_active)
        self.assertTrue(foreign.is_active)
        self.assertNotIn('OTHER', self.catalog())
        log = SyncLog.objects.latest('pk').details
        self.assertTrue({'created', 'updated', 'deactivated', 'errors', 'startedAt', 'finishedAt', 'distributorId', 'syncType'} <= log.keys())

    def test_invalid_snapshot_reports_errors_without_deactivating_omitted_rows(self):
        result = self.sync([{'sku': 'INVALID', 'price': 'NaN'}], fullSync=True).json()
        self.assertTrue(result['errors'])
        self.assertTrue(result['deactivationSkipped'])
        self.product.refresh_from_db()
        self.assertTrue(self.product.is_active)

    def test_revision_covers_manual_queryset_bulk_excel_and_stock_writes(self):
        revisions = [catalog_revision(self.distributor)]
        self.product.price = Decimal(800)
        self.product.save(update_fields=['price'])
        revisions.append(catalog_revision(self.distributor))
        Product.objects.filter(pk=self.product.pk).update(quantity=3, status='low')
        revisions.append(catalog_revision(self.distributor))
        Product.objects.bulk_create([Product(distributor=self.distributor, sku='BULK', name='Bulk', category='К', price=1)])
        revisions.append(catalog_revision(self.distributor))
        upsert_products(self.distributor, [{'sku': self.product.sku, 'name': 'Excel', 'brand': 'Excel', 'category': 'Excel', '_has_price': True, 'price': Decimal(750)}])
        revisions.append(catalog_revision(self.distributor))
        response = self.http.post('/api/integration/erp/stock-update/', [{'sku': self.product.sku, 'quantity': 1, 'price': '700.00'}], content_type='application/json', HTTP_X_INTEGRATION_TOKEN=self.integration.token)
        self.assertEqual(response.status_code, 200)
        revisions.append(catalog_revision(self.distributor))
        self.assertEqual(len(revisions), len(set(revisions)))
        self.assertEqual(self.catalog()[self.product.sku]['quantity'], 1)
        self.assertEqual(self.http.get('/api/catalog-state/', **self.auth).json()['catalogRevision'], revisions[-1])
