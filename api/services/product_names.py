"""Shared normalization for product names, synonyms and search."""

def normalize_search_text(value):
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


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
    # Newlines prevent a query from matching across two unrelated fields.
    return "\n".join(normalize_search_text(value) for value in (
        product.name, product.sku, product.brand, product.category, *product.synonyms,
    ))
