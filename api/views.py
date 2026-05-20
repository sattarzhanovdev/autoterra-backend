import json
import secrets
from datetime import datetime

from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .models import (
    AuthToken,
    ClientProfile,
    ColorRequest,
    CourierTask,
    ExpertTicket,
    KnowledgeCard,
    Notification,
    Order,
    OrderItem,
    Product,
    Purchase,
    Referral,
    Store,
)


def _json(request):
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return {}


def _dt(value):
    if not value:
        return timezone.now()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed)
    return parsed


def _date(value):
    if not value:
        return timezone.localdate()
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date()


def _current_user(request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth.replace("Bearer ", "", 1).strip()
    obj = AuthToken.objects.filter(key=token).select_related("user").first()
    return obj.user if obj else None


def _require_client(request):
    user = _current_user(request)
    if user is None:
        return None, JsonResponse({"detail": "Unauthorized"}, status=401)
    try:
        return user.client_profile, None
    except ClientProfile.DoesNotExist:
        return None, JsonResponse({"detail": "Профиль клиента не создан в admin"}, status=403)


def _format_client(client):
    return {
        "id": str(client.id),
        "inn": client.inn,
        "name": client.company_name,
        "category": client.category,
        "region": client.region,
        "city": client.city,
        "contact": client.contact_name,
        "phone": client.phone,
        "distributorId": str(client.distributor_id),
        "managerId": None,
        "status": client.status,
        "partnerStatus": client.partner_status,
        "totalPurchases": float(client.total_purchases),
        "createdAt": client.created_at.isoformat(),
    }


def _format_distributor(distributor):
    return {
        "id": str(distributor.id),
        "userId": str(distributor.user_id) if distributor.user_id else None,
        "name": distributor.name,
        "inn": distributor.inn,
        "regions": distributor.regions,
        "phone": distributor.phone,
        "email": distributor.email,
        "isActive": distributor.is_active,
    }


def _format_store(store):
    return {
        "id": str(store.id),
        "name": store.name,
        "address": store.address,
        "isActive": store.is_active,
        "createdAt": store.created_at.isoformat(),
    }


def _format_product(product):
    return {
        "id": str(product.id),
        "distributorId": str(product.distributor_id),
        "sku": product.sku,
        "name": product.name,
        "category": product.category,
        "brand": product.brand,
        "volume": float(product.volume),
        "price": float(product.price),
        "quantity": product.quantity,
        "status": product.status,
        "updatedAt": product.updated_at.isoformat(),
    }


def _format_purchase_item(item):
    return {
        "sku": item.sku,
        "name": item.name,
        "category": item.category,
        "quantity": item.quantity,
        "volume": float(item.volume),
        "price": float(item.price),
        "brand": item.brand,
    }


def _format_order_item(item):
    return {
        "sku": item.sku,
        "name": item.name,
        "category": item.category,
        "quantity": item.quantity,
        "volume": float(item.volume),
        "price": float(item.price),
        "brand": item.brand,
    }


def _format_order(order):
    items = [_format_order_item(item) for item in order.items.all()]
    return {
        "id": str(order.id),
        "clientId": str(order.client_id),
        "distributorId": str(order.distributor_id),
        "storeId": str(order.store_id),
        "storeName": order.store.name,
        "documentNumber": f"ORD-{order.id:05d}",
        "date": order.created_at.isoformat(),
        "totalAmount": float(order.total_amount),
        "status": "pending" if order.status in ("pending", "accepted") else "verified",
        "orderStatus": order.status,
        "comment": order.comment,
        "documentUrl": None,
        "createdAt": order.created_at.isoformat(),
        "items": items,
    }


def _format_purchase(purchase):
    return {
        "id": str(purchase.id),
        "clientId": str(purchase.client_id),
        "distributorId": str(purchase.distributor_id),
        "documentNumber": purchase.document_number,
        "date": purchase.date.isoformat(),
        "totalAmount": float(purchase.total_amount),
        "status": purchase.status,
        "documentUrl": purchase.document_url or None,
        "createdAt": purchase.created_at.isoformat(),
        "items": [_format_purchase_item(item) for item in purchase.items.all()],
    }


def _format_color_request(item):
    return {
        "id": str(item.id),
        "clientId": str(item.client_id),
        "carBrand": item.car_brand,
        "carModel": item.car_model,
        "vin": item.vin,
        "colorCode": item.color_code,
        "colorName": item.color_name,
        "urgent": item.urgent,
        "courierPickup": item.courier_pickup,
        "status": item.status,
        "recipe": item.recipe or None,
        "createdAt": item.created_at.isoformat(),
    }


def _format_courier_task(item):
    return {
        "id": str(item.id),
        "clientId": str(item.client_id),
        "type": item.type,
        "address": item.address,
        "scheduledTime": item.scheduled_time.isoformat(),
        "contactName": item.contact_name,
        "contactPhone": item.contact_phone,
        "carDescription": item.car_description,
        "status": item.status,
        "courierId": item.courier_id or None,
        "photoProof": item.photo_proof or None,
        "comment": item.comment or None,
        "createdAt": item.created_at.isoformat(),
    }


def _format_referral(item):
    return {
        "id": str(item.id),
        "inviterId": str(item.inviter_id),
        "inviteeInn": item.invitee_inn,
        "inviteeName": item.invitee_name,
        "region": item.region,
        "isRegistered": item.is_registered,
        "hasPurchase": item.has_purchase,
        "purchaseAmount": float(item.purchase_amount),
        "conditionMet": item.condition_met,
        "gift": item.gift or None,
        "createdAt": item.created_at.isoformat(),
    }


def _format_ticket(item):
    return {
        "id": str(item.id),
        "clientId": str(item.client_id),
        "question": item.question,
        "category": item.category,
        "aiAnswer": item.ai_answer or None,
        "expertAnswer": item.expert_answer or None,
        "status": item.status,
        "createdAt": item.created_at.isoformat(),
    }


def _format_notification(item):
    return {
        "id": str(item.id),
        "title": item.title,
        "body": item.body,
        "type": item.type,
        "isRead": item.is_read,
        "createdAt": item.created_at.isoformat(),
    }


def _format_knowledge_card(item):
    return {
        "id": str(item.id),
        "problem": item.problem,
        "causes": item.causes,
        "solution": item.solution,
        "skus": item.skus,
        "restrictions": item.restrictions or None,
        "approvingExpert": item.approving_expert,
        "isApproved": item.is_approved,
        "createdAt": item.created_at.isoformat(),
    }


@require_GET
def health(_request):
    return JsonResponse({"status": "ok", "service": "autoterra-api"})


@csrf_exempt
@require_POST
def login(request):
    payload = _json(request)
    phone = (payload.get("phone") or "").strip()
    password = payload.get("password") or ""
    normalized = "".join(ch for ch in phone if ch.isdigit() or ch == "+")
    if normalized.startswith("8"):
        normalized = f"+7{normalized[1:]}"

    user = authenticate(username=normalized, password=password)
    if user is None:
        user = User.objects.filter(username=normalized).first()
        if user is None or not user.check_password(password):
            return JsonResponse({"detail": "Неверный телефон или пароль"}, status=401)

    token = secrets.token_hex(24)
    AuthToken.objects.create(key=token, user=user)

    try:
        client = user.client_profile
    except ClientProfile.DoesNotExist:
        distributor = getattr(user, "distributor_profile", None)
        if distributor is None:
            return JsonResponse({"detail": "Профиль клиента или дистрибьютора не создан в admin"}, status=403)
        return JsonResponse(
            {
                "token": token,
                "user": {
                    "id": str(user.id),
                    "phone": user.username,
                    "email": user.email,
                    "role": "distributor",
                    "status": "active" if distributor.is_active else "blocked",
                    "distributor": _format_distributor(distributor),
                },
            }
        )

    return JsonResponse(
        {
            "token": token,
            "user": {
                "id": str(user.id),
                "phone": client.phone,
                "email": user.email,
                "role": "autoservice",
                "status": client.status,
            },
        }
    )


@require_GET
def me(request):
    client, err = _require_client(request)
    if err:
        return err
    return JsonResponse(
        {
            "id": str(client.user_id),
            "phone": client.phone,
            "email": client.user.email,
            "role": "autoservice",
            "client": _format_client(client),
            "distributor": _format_distributor(client.distributor),
        }
    )


@require_GET
def dashboard(request):
    client, err = _require_client(request)
    if err:
        return err
    purchases = client.purchases.prefetch_related("items").order_by("-date")[:2]
    color_requests = client.color_requests.exclude(status="delivered").order_by("-created_at")[:2]
    return JsonResponse(
        {
            "client": _format_client(client),
            "distributor": _format_distributor(client.distributor),
            "unreadCount": client.notifications.filter(is_read=False).count(),
            "recentPurchases": [_format_purchase(item) for item in purchases],
            "activeColorRequests": [_format_color_request(item) for item in color_requests],
        }
    )


@require_GET
def stores(request):
    client, err = _require_client(request)
    if err:
        return err
    return JsonResponse({"results": [_format_store(item) for item in client.stores.filter(is_active=True)]})


@require_GET
def products(request):
    client, err = _require_client(request)
    if err:
        return err
    qs = Product.objects.filter(distributor=client.distributor, is_active=True).order_by("category", "name")
    return JsonResponse({"results": [_format_product(item) for item in qs]})


@require_GET
def order_config(request):
    client, err = _require_client(request)
    if err:
        return err
    return JsonResponse(
        {
            "client": _format_client(client),
            "distributor": _format_distributor(client.distributor),
            "stores": [_format_store(item) for item in client.stores.filter(is_active=True)],
            "products": [
                _format_product(item)
                for item in Product.objects.filter(distributor=client.distributor, is_active=True)
            ],
        }
    )


@require_GET
def orders(request):
    client, err = _require_client(request)
    if err:
        return err
    qs = client.orders.select_related("store", "distributor").prefetch_related("items").order_by("-created_at")
    return JsonResponse({"results": [_format_order(item) for item in qs]})


@csrf_exempt
@require_POST
def create_order(request):
    client, err = _require_client(request)
    if err:
        return err
    payload = _json(request)
    store_id = payload.get("storeId")
    items = payload.get("items") or []
    comment = (payload.get("comment") or "").strip()
    if not store_id or not items:
        return JsonResponse({"detail": "Выберите магазин и товары"}, status=400)

    store = Store.objects.filter(id=store_id, client=client, is_active=True).first()
    if store is None:
        return JsonResponse({"detail": "Магазин не найден"}, status=404)

    product_ids = [item.get("productId") for item in items if item.get("productId")]
    products_by_id = {
        str(product.id): product
        for product in Product.objects.filter(id__in=product_ids, distributor=client.distributor, is_active=True)
    }

    with transaction.atomic():
        order = Order.objects.create(client=client, store=store, distributor=client.distributor, comment=comment)
        for raw in items:
            product = products_by_id.get(str(raw.get("productId")))
            quantity = int(raw.get("quantity") or 0)
            if product is None or quantity <= 0:
                continue
            OrderItem.objects.create(
                order=order,
                product=product,
                sku=product.sku,
                name=product.name,
                category=product.category,
                brand=product.brand,
                volume=product.volume,
                price=product.price,
                quantity=quantity,
            )
        if not order.items.exists():
            transaction.set_rollback(True)
            return JsonResponse({"detail": "Не удалось добавить товары"}, status=400)

    return JsonResponse({"order": _format_order(order)}, status=201)


@require_GET
def purchases(request):
    client, err = _require_client(request)
    if err:
        return err
    qs = client.purchases.prefetch_related("items").order_by("-date")
    return JsonResponse({"results": [_format_purchase(item) for item in qs]})


@csrf_exempt
@require_POST
def create_purchase(request):
    client, err = _require_client(request)
    if err:
        return err
    payload = _json(request)
    purchase = Purchase.objects.create(
        client=client,
        distributor=client.distributor,
        document_number=(payload.get("documentNumber") or "").strip(),
        date=_date(payload.get("date")),
        total_amount=payload.get("totalAmount") or 0,
        status="pending",
        document_url=(payload.get("documentUrl") or "").strip(),
    )
    for raw in payload.get("items") or []:
        PurchaseItem.objects.create(
            purchase=purchase,
            sku=raw.get("sku") or "",
            name=raw.get("name") or "",
            category=raw.get("category") or "",
            quantity=raw.get("quantity") or 1,
            volume=raw.get("volume") or 0,
            price=raw.get("price") or 0,
            brand=raw.get("brand") or "AutoTerra",
        )
    return JsonResponse({"purchase": _format_purchase(purchase)}, status=201)


@require_GET
def color_requests(request):
    client, err = _require_client(request)
    if err:
        return err
    return JsonResponse({"results": [_format_color_request(item) for item in client.color_requests.all()]})


@csrf_exempt
@require_POST
def create_color_request(request):
    client, err = _require_client(request)
    if err:
        return err
    payload = _json(request)
    item = ColorRequest.objects.create(
        client=client,
        car_brand=(payload.get("carBrand") or "").strip(),
        car_model=(payload.get("carModel") or "").strip(),
        vin=(payload.get("vin") or "").strip(),
        color_code=(payload.get("colorCode") or "").strip(),
        color_name=(payload.get("colorName") or "").strip(),
        urgent=bool(payload.get("urgent")),
        courier_pickup=bool(payload.get("courierPickup")),
    )
    return JsonResponse({"request": _format_color_request(item)}, status=201)


@require_GET
def courier_tasks(request):
    client, err = _require_client(request)
    if err:
        return err
    return JsonResponse({"results": [_format_courier_task(item) for item in client.courier_tasks.all()]})


@csrf_exempt
@require_POST
def create_courier_task(request):
    client, err = _require_client(request)
    if err:
        return err
    payload = _json(request)
    item = CourierTask.objects.create(
        client=client,
        type=payload.get("type") or "delivery",
        address=(payload.get("address") or "").strip(),
        scheduled_time=_dt(payload.get("scheduledTime")),
        contact_name=(payload.get("contactName") or client.contact_name).strip(),
        contact_phone=(payload.get("contactPhone") or client.phone).strip(),
        car_description=(payload.get("carDescription") or "").strip(),
        comment=(payload.get("comment") or "").strip(),
    )
    return JsonResponse({"task": _format_courier_task(item)}, status=201)


@require_GET
def referrals(request):
    client, err = _require_client(request)
    if err:
        return err
    return JsonResponse({"results": [_format_referral(item) for item in client.referrals.all()]})


@csrf_exempt
@require_POST
def create_referral(request):
    client, err = _require_client(request)
    if err:
        return err
    payload = _json(request)
    item = Referral.objects.create(
        inviter=client,
        invitee_inn=(payload.get("inviteeInn") or "").strip(),
        invitee_name=(payload.get("inviteeName") or "").strip(),
        region=(payload.get("region") or client.region).strip(),
    )
    return JsonResponse({"referral": _format_referral(item)}, status=201)


@require_GET
def tickets(request):
    client, err = _require_client(request)
    if err:
        return err
    return JsonResponse({"results": [_format_ticket(item) for item in client.expert_tickets.all()]})


@csrf_exempt
@require_POST
def create_ticket(request):
    client, err = _require_client(request)
    if err:
        return err
    payload = _json(request)
    item = ExpertTicket.objects.create(
        client=client,
        question=(payload.get("question") or "").strip(),
        category=(payload.get("category") or "Общий вопрос").strip(),
    )
    return JsonResponse({"ticket": _format_ticket(item)}, status=201)


@require_GET
def notifications(request):
    client, err = _require_client(request)
    if err:
        return err
    return JsonResponse({"results": [_format_notification(item) for item in client.notifications.all()]})


@csrf_exempt
@require_POST
def mark_notifications_read(request):
    client, err = _require_client(request)
    if err:
        return err
    client.notifications.filter(is_read=False).update(is_read=True)
    return JsonResponse({"ok": True})


@require_GET
def knowledge_cards(request):
    client, err = _require_client(request)
    if err:
        return err
    qs = KnowledgeCard.objects.filter(is_approved=True)
    return JsonResponse({"results": [_format_knowledge_card(item) for item in qs]})
