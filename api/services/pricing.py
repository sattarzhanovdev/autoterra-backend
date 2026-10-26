"""Цена товара для конкретного клиента.

Активная персональная цена ClientPriceOverride имеет приоритет. Без неё
применяется скидка клиента по бренду, затем общая персональная скидка,
а если они не заданы — расчёт
по рангу ClientProfile.partner_status (PartnerTier и RankDiscount).

``price_details`` / ``price_for_client`` задают клиентскую цену до скидки
за объём заказа. ``order_discounts.quote_order`` дополняет этот расчёт
одинаково для корзины, создания и корректировки заказа.
"""

from decimal import ROUND_HALF_UP, ROUND_CEILING, Decimal

MAX_DISCOUNT_PERCENT = Decimal("40")

TWO_PLACES = Decimal("0.01")


def _rules_for(distributor_id):
    """Активные правила дистрибьютора плюс глобальные, одним запросом."""
    from django.db.models import Q

    from api.models import RankDiscount

    scope = Q(distributor_id=None)
    if distributor_id:
        scope |= Q(distributor_id=distributor_id)
    return list(
        RankDiscount.objects
        .filter(scope, is_active=True, tier__is_active=True)
        .select_related("tier")
    )


def _cached_rules(client, distributor_id):
    """Правила читаем один раз на клиента: страница каталога — это 20+ товаров
    одного дистрибьютора, и запрос на каждый превратился бы в N+1."""
    cache = getattr(client, "_rank_discount_cache", None)
    if cache is None:
        cache = {}
        try:
            client._rank_discount_cache = cache
        except AttributeError:  # объект без __dict__ — обойдёмся без кэша
            return _rules_for(distributor_id)
    if distributor_id not in cache:
        cache[distributor_id] = _rules_for(distributor_id)
    return cache[distributor_id]


def discount_percent(client, product) -> Decimal:
    """Скидка в процентах для пары «клиент — товар». Нет правила — ноль.

    Ищем от частного к общему: правило дистрибьютора важнее глобального,
    правило на категорию важнее правила на весь ассортимент.
    """
    if client is None or product is None:
        return Decimal("0")

    tier_name = (getattr(client, "partner_status", "") or "").strip()
    if not tier_name:
        return Decimal("0")

    distributor_id = getattr(product, "distributor_id", None)
    product_category = (getattr(product, "category", "") or "").strip()

    candidates = [
        rule
        for rule in _cached_rules(client, distributor_id)
        if rule.tier.name == tier_name
        and rule.product_category in ("", product_category)
    ]
    if not candidates:
        return Decimal("0")

    # Сортируем по «специфичности»: сначала правило дистрибьютора, затем
    # правило на конкретную категорию товаров.
    candidates.sort(
        key=lambda rule: (
            rule.distributor_id is not None,
            bool(rule.product_category),
        ),
        reverse=True,
    )
    return Decimal(str(candidates[0].percent))


def apply_discount(price, percent) -> Decimal:
    """Цена со скидкой, округлённая до копеек."""
    base = Decimal(str(price or 0))
    pct = min(MAX_DISCOUNT_PERCENT, max(Decimal("0"), Decimal(str(percent or 0))))
    if pct <= 0:
        return base.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    result = base * (Decimal("100") - pct) / Decimal("100")
    floor = (base * (100 - MAX_DISCOUNT_PERCENT) / 100).quantize(TWO_PLACES, rounding=ROUND_CEILING)
    return max(floor, result.quantize(TWO_PLACES, rounding=ROUND_HALF_UP))


def price_for_client(client, product) -> Decimal:
    """Персональная цена → бренд → общая скидка → ранг → базовая цена."""
    return price_details(client, product)["price"]


def personal_price(client, product):
    """One query per client instance/request, never one query per catalog row."""
    if client is None or product is None or client.distributor_id != product.distributor_id:
        return None
    cache = getattr(client, "_personal_prices", None)
    if cache is None:
        from api.models import ClientPriceOverride
        cache = dict(ClientPriceOverride.objects.filter(
            client_id=client.pk, is_active=True,
            product__distributor_id=client.distributor_id,
        ).values_list("product_id", "price"))
        client._personal_prices = cache
    return cache.get(product.pk)



def brand_discount_percent(client, product):
    if client is None or product is None or client.distributor_id != product.distributor_id:
        return None
    from .product_names import normalize_brand
    cache = getattr(client, "_brand_discounts", None)
    if cache is None:
        from api.models import ClientBrandDiscount
        cache = dict(ClientBrandDiscount.objects.filter(client_id=client.pk).values_list("brand", "percent"))
        client._brand_discounts = cache
    return cache.get(normalize_brand(product.brand))


def price_details(client, product) -> dict:
    """Цена + из чего она получилась — для витрины и отладки."""
    personal_discount = getattr(client, "personal_discount_percent", None)
    brand_discount = brand_discount_percent(client, product)
    percent = brand_discount if brand_discount is not None else personal_discount if personal_discount is not None else discount_percent(client, product)
    base = Decimal(str(product.price or 0)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    percent = min(MAX_DISCOUNT_PERCENT, max(Decimal("0"), Decimal(str(percent))))
    final = apply_discount(product.price, percent)
    override = personal_price(client, product)
    if override is not None:
        final = max(override, (base * (100 - MAX_DISCOUNT_PERCENT) / 100).quantize(TWO_PLACES, rounding=ROUND_CEILING))
        percent = Decimal("0")  # Percentage discounts never stack with an override.
    return {
        "price": final,
        "base_price": base,
        "discount_percent": percent,
        "has_discount": final < base,
        "effective_discount_percent": max(Decimal("0"), (base - final) / base * 100) if base > 0 else Decimal("0"),
        "personal_price": override,
        "price_source": "personal" if override is not None else "brand_discount" if brand_discount is not None else "personal_discount" if personal_discount is not None else "rank" if percent > 0 else "base",
    }
