from django.db import migrations


def backfill_search_text(apps, schema_editor):
    # Freeze the normalization/layout used by this migration; do not import
    # live application helpers whose behavior may change in future releases.
    lookalikes = str.maketrans("авекмнорстух", "abekmhopctyx")

    def normalize(value):
        value = str(value or "").casefold().replace("ё", "е").translate(lookalikes)
        return " ".join("".join(c if c.isalpha() or c.isdigit() else " " for c in value).split())

    products = apps.get_model("api", "Product").objects.using(schema_editor.connection.alias)
    last_pk = 0
    while True:
        batch = list(products.filter(pk__gt=last_pk).order_by("pk").only(
            "pk", "sku", "name", "brand", "category", "synonyms",
        )[:500])
        if not batch:
            break
        for product in batch:
            lines = [normalize(value) for value in (
                product.name, product.sku, product.brand, product.category, *product.synonyms,
            )]
            product.search_text = "\n".join([
                normalize(product.sku).replace(" ", ""), *lines,
                normalize(product.name).replace(" ", ""),
            ])
        products.bulk_update(batch, ["search_text"], batch_size=500)
        last_pk = batch[-1].pk


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("api", "0059_knowledge_search_indexes")]
    operations = [migrations.RunPython(backfill_search_text, migrations.RunPython.noop)]
