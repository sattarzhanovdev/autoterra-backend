from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from api.models import Product
from api.services.product_names import product_search_text


class Command(BaseCommand):
    help = "Rebuild normalized product search text for every distributor (including inactive products)."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=500)
        parser.add_argument("--database", default="default", choices=tuple(connections))

    def handle(self, *args, **options):
        size = options["batch_size"]
        if size < 1:
            raise CommandError("--batch-size must be positive")
        products = Product.objects.using(options["database"])
        last_pk = 0
        total = 0
        while True:
            batch = list(products.filter(pk__gt=last_pk).order_by("pk").only(
                "pk", "sku", "name", "brand", "category", "synonyms",
            )[:size])
            if not batch:
                break
            for product in batch:
                product.search_text = product_search_text(product)
            products.bulk_update(batch, ["search_text"], batch_size=size)
            last_pk = batch[-1].pk
            total += len(batch)
        self.stdout.write(self.style.SUCCESS(f"Rebuilt search text for {total} products."))
