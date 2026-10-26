"""Current-order volume pricing. Money stays Decimal until JSON serialization."""
from decimal import Decimal, ROUND_HALF_UP, ROUND_CEILING

from .pricing import MAX_DISCOUNT_PERCENT, TWO_PLACES, price_details

ORDER_DISCOUNT_FIRST_THRESHOLD = Decimal('10000')
ORDER_DISCOUNT_FIRST_PERCENT = Decimal('3')
ORDER_DISCOUNT_STEP_AMOUNT = Decimal('5000')
ORDER_DISCOUNT_STEP_PERCENT = Decimal('0.5')


def money(value):
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def order_discount_config():
    return dict(firstThreshold=ORDER_DISCOUNT_FIRST_THRESHOLD,
                firstPercent=ORDER_DISCOUNT_FIRST_PERCENT,
                stepAmount=ORDER_DISCOUNT_STEP_AMOUNT,
                stepPercent=ORDER_DISCOUNT_STEP_PERCENT,
                maxDiscountPercent=MAX_DISCOUNT_PERCENT)


def order_discount_details(subtotal):
    subtotal = money(Decimal(str(subtotal)))
    current_threshold = Decimal('0')
    percent = Decimal('0')
    next_threshold = ORDER_DISCOUNT_FIRST_THRESHOLD
    next_percent = ORDER_DISCOUNT_FIRST_PERCENT
    if subtotal >= ORDER_DISCOUNT_FIRST_THRESHOLD:
        steps = (subtotal - ORDER_DISCOUNT_FIRST_THRESHOLD) // ORDER_DISCOUNT_STEP_AMOUNT
        percent = min(MAX_DISCOUNT_PERCENT, ORDER_DISCOUNT_FIRST_PERCENT + steps * ORDER_DISCOUNT_STEP_PERCENT)
        current_threshold = ORDER_DISCOUNT_FIRST_THRESHOLD + steps * ORDER_DISCOUNT_STEP_AMOUNT
        next_threshold = current_threshold + ORDER_DISCOUNT_STEP_AMOUNT
        next_percent = min(MAX_DISCOUNT_PERCENT, percent + ORDER_DISCOUNT_STEP_PERCENT)
    maximum = percent >= MAX_DISCOUNT_PERCENT
    return dict(subtotalBeforeOrderDiscount=subtotal, orderDiscountPercent=percent,
                nextDiscountPercent=None if maximum else next_percent,
                nextThreshold=None if maximum else next_threshold,
                amountToNextThreshold=Decimal('0') if maximum else next_threshold - subtotal,
                maxDiscountPercent=MAX_DISCOUNT_PERCENT, maximumReached=maximum,
                progress=Decimal('1') if maximum else (subtotal - current_threshold) / (next_threshold - current_threshold))


def quote_order(client, products, quantities):
    """Use the existing client price first; contractual prices receive no bonus."""
    details = {pid: price_details(client, products[pid]) for pid in quantities}
    subtotal = sum((details[pid]['price'] * qty for pid, qty in quantities.items()), Decimal('0'))
    result = order_discount_details(subtotal)
    percent = result['orderDiscountPercent']
    prices = {}
    capped = []
    for pid in quantities:
        item = details[pid]
        base, price = item['base_price'], item['price']
        if item['price_source'] != 'personal':
            # Subtract percentage points of the base price, not a percentage
            # of the already discounted price. Round once per saved unit.
            floor = (base * (100 - MAX_DISCOUNT_PERCENT) / 100).quantize(TWO_PLACES, rounding=ROUND_CEILING)
            price = max(floor, money(price - base * percent / 100))
            capped.append(base <= 0 or price <= floor)
        prices[pid] = price
    all_contracts_at_cap = bool(details) and all(
        item['base_price'] > 0 and item['effective_discount_percent'] == MAX_DISCOUNT_PERCENT
        for item in details.values())
    maximum = (bool(capped) and all(capped)) or all_contracts_at_cap
    result['maximumReached'] = maximum
    if result['maximumReached']:
        result.update(nextDiscountPercent=None, nextThreshold=None, amountToNextThreshold=Decimal('0'), progress=Decimal('1'))
    # A basket consisting solely of contractual prices cannot gain a volume discount.
    result['hasEligibleItems'] = bool(capped)
    if not capped:
        result.update(nextDiscountPercent=None, nextThreshold=None, amountToNextThreshold=Decimal('0'))
        if not result['maximumReached']:
            result['progress'] = Decimal('0')
    total = sum((prices[pid] * qty for pid, qty in quantities.items()), Decimal('0'))
    result.update(total=money(total), discountAmount=money(subtotal - total))
    return prices, result
