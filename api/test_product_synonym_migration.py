from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class ProductSynonymMigrationTests(TransactionTestCase):
    def test_existing_assortment_is_searchable_for_all_distributors(self):
        before = [('api', '0054_yookassa_finance')]
        after = [('api', '0055_product_synonyms')]
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        try:
            old_apps = executor.loader.project_state(before).apps
            Distributor = old_apps.get_model('api', 'Distributor')
            Product = old_apps.get_model('api', 'Product')
            ids = []
            for index in range(2):
                distributor = Distributor.objects.create(name=f'Dist {index}', inn=str(index))
                product = Product.objects.create(distributor=distributor, sku='FILM',
                    name='ЗаЩиТнАя ПлЁнКа', category='Материалы', price=123, quantity=9,
                    images=['https://example.com/a.jpg'])
                ids.append(product.pk)
            executor = MigrationExecutor(connection)
            executor.migrate(after)
            from .models import Product
            from .services.search import search_products
            products = list(search_products(Product.objects.all(), 'защитная пленка'))
            self.assertEqual({p.pk for p in products}, set(ids))
            for product in products:
                self.assertEqual(product.synonyms, [])
                self.assertEqual(product.price, 123)
                self.assertEqual(product.quantity, 9)
                self.assertEqual(product.images, ['https://example.com/a.jpg'])
        finally:
            MigrationExecutor(connection).migrate(after)
