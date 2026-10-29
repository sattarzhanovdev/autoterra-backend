from .models import Distributor, Product
from .test_bonus_account import _Base


class CatalogBrandFilterTests(_Base):
    """Brands cover the full available catalog, never a product page."""

    def setUp(self):
        super().setUp()
        self.auth = {'HTTP_AUTHORIZATION': f'Bearer {self.token}'}

    def config(self):
        response = self.http.get('/api/order-config/', **self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Cache-Control'], 'private, no-store')
        return response.json()

    def test_brands_cover_all_pages_and_exclude_unavailable_catalogs(self):
        for i in range(25):
            Product.objects.create(distributor=self.distributor, sku=f'BR-{i}', name=f'Tовар {i}', brand=f'Бренд {i}', category='Материалы')
        Product.objects.create(distributor=self.distributor, sku='EMPTY', name='Без бренда', brand='  ')
        Product.objects.create(distributor=self.distributor, sku='HIDDEN', name='Скрыт', brand='Скрытый', is_active=False)
        other = Distributor.objects.create(name='Чужой', inn='9999999999')
        Product.objects.create(distributor=other, sku='FOREIGN', name='Чужой', brand='Чужой бренд')
        brands = self.config()['brands']
        for i in range(25):
            self.assertIn(f'Бренд {i}', brands)
        for missing in ('', '  ', 'Скрытый', 'Чужой бренд'):
            self.assertNotIn(missing, brands)

    def test_case_and_whitespace_variants_share_one_working_filter(self):
        products = [Product.objects.create(distributor=self.distributor, sku=f'VAR-{i}', name=f'Материал {i}', brand=brand)
                    for i, brand in enumerate((' Novol ', 'NOVOL', 'novol', 'NoVoL', 'Novol\u00a0'))]
        brands = [name for name in self.config()['brands'] if name.casefold() == 'novol']
        self.assertEqual(len(brands), 1)
        for value in (brands[0], 'NOVOL', ' novol '):
            response = self.http.get('/api/products/', {'brand': value}, **self.auth)
            self.assertEqual(response.status_code, 200)
            self.assertEqual({row['id'] for row in response.json()['results']}, {str(p.pk) for p in products})

    def test_new_and_removed_brand_reflected_without_stale_cache(self):
        self.assertNotIn('Свежий бренд', self.config()['brands'])
        product = Product.objects.create(distributor=self.distributor, sku='NEW-BRAND', name='Новый товар', brand='Свежий бренд', quantity=0, status='onOrder')
        self.assertIn('Свежий бренд', self.config()['brands'])
        Product.objects.filter(pk=product.pk).update(is_active=False)
        self.assertNotIn('Свежий бренд', self.config()['brands'])
