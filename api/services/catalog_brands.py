"""Brand facets and matching share the same scoped, active catalog."""
import unicodedata

from .product_names import normalize_brand


def brand_variants(products):
    groups = {}
    for raw in products.order_by('brand').values_list('brand', flat=True).distinct():
        key = normalize_brand(raw)
        if key:
            groups.setdefault(key, []).append(raw)
    return groups


def catalog_brands(products):
    labels = [
        ' '.join(unicodedata.normalize('NFKC', values[0]).split())
        for values in brand_variants(products).values()
    ]
    return sorted(labels, key=normalize_brand)


def filter_catalog_brand(products, brand):
    # Preserve actual stored values for SQL matching; don't rewrite price lists.
    variants = brand_variants(products).get(normalize_brand(brand), [])
    return products.filter(brand__in=variants)
