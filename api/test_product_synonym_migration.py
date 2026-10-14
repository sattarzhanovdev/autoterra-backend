from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class ProductSynonymMigrationTests(TransactionTestCase):
    def test_existing_assortment_is_searchable_for_all_distributors(self):
        before = [('api', '0054_yookassa_finance')]
        after = [('api', '0060_product_search_normalization')]
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
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
            MigrationExecutor(connection).migrate(latest)


    def test_normalization_backfill_crosses_batch_boundary_and_keeps_product_data(self):
        before = [("api", "0059_knowledge_search_indexes")]
        after = [("api", "0060_product_search_normalization")]
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        executor.migrate(before)
        try:
            apps = executor.loader.project_state(before).apps
            Distributor = apps.get_model("api", "Distributor")
            Product = apps.get_model("api", "Product")
            distributors = [Distributor.objects.create(name=f"Batch {i}", inn=str(i)) for i in range(2)]
            Product.objects.bulk_create([
                Product(distributor=distributors[i % 2], sku=f"13-10-{i}",
                    name="Шлифблок (мягкий) 230х280", synonyms=["Лёгкий 3М Р120"],
                    search_text="old text", is_active=bool(i % 2), price=123, quantity=9)
                for i in range(501)
            ])
            MigrationExecutor(connection).migrate(after)
            from .models import Product
            from .services.product_names import product_search_text
            from .services.search import search_products
            self.assertEqual(Product.objects.count(), 501)
            for product in Product.objects.all():
                self.assertEqual(product.search_text, product_search_text(product))
                self.assertEqual(product.price, 123)
                self.assertEqual(product.quantity, 9)
                self.assertEqual(product.synonyms, ["Лёгкий 3М Р120"])
            for query in ("мягкий шлифблок", "230x280", "3M P120", "1310500"):
                self.assertTrue(search_products(Product.objects.all(), query).exists())
        finally:
            MigrationExecutor(connection).migrate(latest)
