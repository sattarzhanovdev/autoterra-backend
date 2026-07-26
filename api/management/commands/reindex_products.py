"""Переиндексация товаров в Elasticsearch.

Использование:
    python manage.py reindex_products

Без настроенного ELASTICSEARCH_URL команда ничего не делает и сообщает об этом.
"""

from django.core.management.base import BaseCommand

from api.models import Product
from api.services import search


class Command(BaseCommand):
    help = "Переиндексирует активные товары в Elasticsearch"

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
            default=500,
            help="Сколько документов отправлять за один запрос _bulk",
        )

    def handle(self, *args, **options):
        if not search.is_elasticsearch_enabled():
            self.stdout.write(
                self.style.WARNING(
                    "ELASTICSEARCH_URL не задан — поиск работает средствами БД, "
                    "индексировать нечего."
                )
            )
            return

        search.ensure_index()

        batch_size = options["batch_size"]
        qs = Product.objects.filter(is_active=True).order_by("id")
        total = 0
        batch = []
        for product in qs.iterator(chunk_size=batch_size):
            batch.append(product)
            if len(batch) >= batch_size:
                total += search.bulk_index(batch)
                batch = []
        if batch:
            total += search.bulk_index(batch)

        self.stdout.write(self.style.SUCCESS(f"Проиндексировано товаров: {total}"))
