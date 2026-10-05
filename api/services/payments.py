"""YooKassa (ЮKassa / YooMoney для бизнеса) payment integration.

Dependency-free client built on urllib to match the rest of the codebase.
Docs: https://yookassa.ru/developers/api

Flow
────
1. Клиент нажимает «Оплатить» → create_payment() создаёт платёж в ЮKassa
   с confirmation.type = "redirect" и возвращает confirmation_url.
2. Клиент оплачивает по ссылке → ЮKassa шлёт webhook `payment.succeeded`.
3. Webhook, карточка заказа и фоновая сверка проверяют ответ API ЮKassa
   перед изменением статуса заказа.

Все суммы в рублях, две десятичных цифры (требование ЮKassa).
"""

from __future__ import annotations

import base64
from http.client import HTTPException
import json
import logging
import ssl
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import uuid
from decimal import Decimal, InvalidOperation

from django.conf import settings

try:
    import certifi
except ImportError:  # pragma: no cover
    certifi = None

logger = logging.getLogger(__name__)


class PaymentConfigError(RuntimeError):
    """Raised when YooKassa credentials are missing."""


class PaymentProviderError(RuntimeError):
    """Raised when the YooKassa API returns an error."""


class PaymentUncertainError(PaymentProviderError):
    """The request may have reached the provider. Keep its idempotence key."""


class PaymentReceiptError(ValueError):
    """The order cannot be represented as a valid itemized receipt."""


def is_configured() -> bool:
    return bool(settings.YOOKASSA_SHOP_ID and settings.YOOKASSA_SECRET_KEY)


def _auth_header() -> str:
    raw = f"{settings.YOOKASSA_SHOP_ID}:{settings.YOOKASSA_SECRET_KEY}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _ssl_context():
    if certifi is not None:
        return ssl.create_default_context(cafile=certifi.where())
    return ssl.create_default_context()


def payment_body(order, amount, description=""):
    payable = Decimal(amount).quantize(Decimal("0.01"))
    order_items = list(order.items.order_by("pk"))
    quantities = [item.quantity for item in order_items]
    original_cents = [int(item.price * 100) for item in order_items]
    payable_cents = int(payable * 100)
    minimum = sum(quantities)
    original_total = sum(price * quantity for price, quantity in zip(original_cents, quantities))
    if (not order_items or payable_cents < minimum or payable_cents > original_total
            or any(price < 1 or quantity < 1 for price, quantity in zip(original_cents, quantities))):
        raise PaymentReceiptError("Невозможно составить чек из товаров заказа на сумму платежа")

    # Every unit must have a positive price. Allocate the remaining kopecks
    # proportionally to each line's available value, then split a line into
    # at most two price tiers when its total is not divisible by quantity.
    capacities = [(price - 1) * quantity for price, quantity in zip(original_cents, quantities)]
    extra = payable_cents - minimum
    capacity_total = sum(capacities)
    if capacity_total:
        shares = [divmod(extra * capacity, capacity_total) for capacity in capacities]
        allocated = [share[0] for share in shares]
        remainder = extra - sum(allocated)
        for index in sorted(range(len(shares)), key=lambda i: (-shares[i][1], i))[:remainder]:
            allocated[index] += 1
    else:
        allocated = [0] * len(order_items)

    receipt_items = []
    for item, quantity, line_extra in zip(order_items, quantities, allocated):
        unit_cents, higher_count = divmod(quantity + line_extra, quantity)
        for count, price_cents in ((quantity - higher_count, unit_cents), (higher_count, unit_cents + 1)):
            if count:
                receipt_items.append({
                    "description": item.name,
                    "quantity": count,
                    "amount": {"value": f"{Decimal(price_cents) / 100:.2f}", "currency": "RUB"},
                    "vat_code": 7,
                    "payment_mode": "full_prepayment",
                    "payment_subject": "commodity",
                    "measure": "piece",
                })

    email = (order.client.user.email or "").strip() or settings.YOOKASSA_RECEIPT_EMAIL.strip()
    if not email:
        raise PaymentReceiptError("Не указан email для чека ЮKassa")
    return {
        "amount": {
            "value": f"{payable:.2f}",
            "currency": "RUB",
        },
        "receipt": {"customer": {"email": email}, "items": receipt_items},
        "capture": True,  # одностадийная оплата: списываем сразу
        "confirmation": {
            "type": "redirect",
            "return_url": settings.YOOKASSA_RETURN_URL,
        },
        "description": description or f"Заказ ORD-{order.id:05d}",
        "metadata": {
            "order_id": str(order.id),
        },
    }


def create_payment(order, amount: Decimal, idempotence_key: str, description: str = "", request_body=None) -> dict:
    """Create a YooKassa payment and return the parsed JSON response.

    Raises PaymentConfigError if creds are missing, PaymentProviderError on API error.
    """
    if not is_configured():
        raise PaymentConfigError("YOOKASSA_SHOP_ID / YOOKASSA_SECRET_KEY не заданы")

    body = request_body or payment_body(order, amount, description)

    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{settings.YOOKASSA_API_URL}/payments",
        data=data,
        method="POST",
        headers={
            "Authorization": _auth_header(),
            "Idempotence-Key": idempotence_key,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20, context=_ssl_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")
        logger.error("YooKassa create_payment failed %s: %s", exc.code, detail)
        error = PaymentUncertainError if exc.code >= 500 or exc.code in (408, 409, 429) or "Idempotence" in detail else PaymentProviderError
        raise error(f"YooKassa error {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, HTTPException) as exc:
        logger.error("YooKassa create_payment network error: %s", exc)
        raise PaymentUncertainError("Не удалось получить ответ ЮKassa") from exc


def fetch_payment(provider_payment_id: str) -> dict:
    """Fetch a payment's current state from YooKassa (used to verify webhooks)."""
    if not is_configured():
        raise PaymentConfigError("YOOKASSA credentials not set")
    req = urllib.request.Request(
        f"{settings.YOOKASSA_API_URL}/payments/{provider_payment_id}",
        method="GET",
        headers={"Authorization": _auth_header()},
    )
    try:
        with urllib.request.urlopen(req, timeout=20, context=_ssl_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")
        error = PaymentUncertainError if exc.code >= 500 or exc.code in (408, 409, 429) or "Idempotence" in detail else PaymentProviderError
        raise error(f"YooKassa error {exc.code}") from exc

    except (urllib.error.URLError, TimeoutError, OSError, ValueError, HTTPException) as exc:
        raise PaymentProviderError("Не удалось проверить платёж") from exc


def new_idempotence_key() -> str:
    return uuid.uuid4().hex


def validate_payment(payment, response):
    """Only authenticated provider responses with matching financial data count."""
    try:
        amount = response["amount"]
        confirmation = response.get("confirmation")
        if confirmation is not None and not isinstance(confirmation, dict):
            raise ValueError("Invalid confirmation")
        url = (confirmation or {}).get("confirmation_url")
        if url is not None:
            if not isinstance(url, str) or len(url) > 512:
                raise ValueError("Invalid confirmation URL")
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("Invalid confirmation URL")
        valid = (
            isinstance(response["id"], str)
            and 0 < len(response["id"]) <= 128
            and (not payment.provider_payment_id or response["id"] == payment.provider_payment_id)
            and Decimal(amount["value"]) == payment.amount
            and amount["currency"] == payment.currency
            and str(response["metadata"]["order_id"]) == str(payment.order_id)
            and str(response["recipient"]["account_id"]) == str(settings.YOOKASSA_SHOP_ID)
            and response["status"] in {"pending", "waiting_for_capture", "succeeded", "canceled"}
            and (response["status"] != "succeeded" or response.get("paid") is True)
        )
    except (KeyError, TypeError, AttributeError, InvalidOperation, ValueError):
        valid = False
    if not valid:
        raise PaymentUncertainError("Ответ ЮKassa не соответствует платежу")
