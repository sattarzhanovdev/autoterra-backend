"""Technical-search backfill keeps existing catalog business data intact."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class TechnicalSearchMigrationTests(TransactionTestCase):
    def test_description_and_group_backfill_across_batches(self):
        before = [('api', '0062_registration_email_delivery')]
        after = [('api', '0063_product_technical_search')]
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        executor.migrate(before)
        try:
            apps = executor.loader.project_state(before).apps
            Distributor = apps.get_model('api', 'Distributor')
            Product = apps.get_model('api', 'Product')
            distributor = Distributor.objects.create(name='Migration fixture', inn='1234567890')
            Product.objects.bulk_create([Product(distributor=distributor, sku=f'TEST-{i}', name='Материал',
                category='Материалы', description='Для подготовки ПлАстИкОвОй поверхности',
                group_name='Специальные покрытия', price='123.45', quantity=19, search_text='old') for i in range(501)])
            MigrationExecutor(connection).migrate(after)
            from .models import Product as CurrentProduct
            from .services.product_names import product_search_text
            from .services.search import search_products
            self.assertEqual(CurrentProduct.objects.count(), 501)
            self.assertEqual(search_products(CurrentProduct.objects.all(), 'пластиковой поверхности').count(), 501)
            self.assertEqual(search_products(CurrentProduct.objects.all(), 'специальные покрытия').count(), 501)
            for product in CurrentProduct.objects.all():
                self.assertEqual(product.search_text, product_search_text(product))
                self.assertEqual(str(product.price), '123.45')
                self.assertEqual(product.quantity, 19)
        finally:
            MigrationExecutor(connection).migrate(latest)
