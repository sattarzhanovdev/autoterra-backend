"""Shared normalization for product names, synonyms and search."""

_LOOKALIKES = str.maketrans("авекмнорстух", "abekmhopctyx")


def normalize_search_text(value):
    value = str(value or "").casefold().replace("ё", "е").translate(_LOOKALIKES)
    return " ".join("".join(c if c.isalpha() or c.isdigit() else " " for c in value).split())


def compact_search_text(value):
    return normalize_search_text(value).replace(" ", "")


def normalize_product_synonyms(value):
    if isinstance(value, str):
        value = value.split(";")
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or any(not isinstance(v, str) for v in value):
        raise ValueError("Синонимы: передайте список строк или строку через «;»")
    result, seen = [], set()
    for item in value:
        item = " ".join(item.split())
        key = normalize_search_text(item)
        if key and key not in seen:
            result.append(item)
            seen.add(key)
    return result


def product_search_text(product):
    # Fixed first two lines let SQL extract the compact SKU and normalized name
    # for ranking without database-specific Unicode normalization functions.
    lines = [normalize_search_text(value) for value in (
        product.name, product.sku, product.brand, product.category, *product.synonyms,
    )]
    return "\n".join([compact_search_text(product.sku), *lines, compact_search_text(product.name)])


def normalize_brand(value):
    """Canonical identity, independent of price-list casing and whitespace."""
    import unicodedata
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())
