"""Цена товара для конкретного клиента.

Прайс один, но платят по нему по-разному: чем больше клиент закупает, тем
выше его ранг (ClientProfile.partner_status) и ниже цена. Пороги рангов и
скидки настраиваются в админке — модели PartnerTier и RankDiscount.

Единственная точка расчёта — ``price_for_client``. И витрина, и оформление
заказа зовут её, поэтому цена в каталоге и цена в заказе не могут разойтись.
"""

from decimal import ROUND_HALF_UP, Decimal

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
    pct = Decimal(str(percent or 0))
    if pct <= 0:
        return base.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    result = base * (Decimal("100") - pct) / Decimal("100")
    return max(Decimal("0"), result).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def price_for_client(client, product) -> Decimal:
    """Итоговая цена товара для клиента с учётом его ранга."""
    return apply_discount(product.price, discount_percent(client, product))


def price_details(client, product) -> dict:
    """Цена + из чего она получилась — для витрины и отладки."""
    percent = discount_percent(client, product)
    base = Decimal(str(product.price or 0)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    final = apply_discount(product.price, percent)
    return {
        "price": final,
        "base_price": base,
        "discount_percent": percent,
        "has_discount": percent > 0,
    }
