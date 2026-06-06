import json
import secrets
import ssl
import urllib.error
import urllib.request
import csv
import io
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256

from django.contrib.auth import authenticate, login as django_login
from django.contrib.contenttypes.models import ContentType
from django.contrib.auth.models import User
from django.conf import settings
from django.db import models, transaction
from django.db.utils import IntegrityError
from django.db.models import Q
from django.http import JsonResponse, HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from .models import (
    Attachment,
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
    PurchaseItem,
    Region,
    Referral,
    RecipeMaterial,
    Store,
)
from .serializers import RegistrationSerializer, PurchaseSerializer

try:
    import certifi
except ImportError:
    certifi = None


MAX_UPLOAD_SIZE = 10 * 1024 * 1024
ALLOWED_UPLOAD_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".pdf", ".mp4", ".mov"}
ALLOWED_UPLOAD_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "application/pdf",
    "video/mp4",
    "video/quicktime",
}


def _json(request):
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return {}


def _normalize_phone(phone):
    normalized = "".join(ch for ch in (phone or "").strip() if ch.isdigit() or ch == "+")
    if normalized.startswith("8"):
        normalized = f"+7{normalized[1:]}"
    return normalized


def _normalize_inn(inn):
    return "".join(ch for ch in (inn or "").strip() if ch.isdigit())


def _normalize_category(category):
    value = (category or "b").strip().lower()
    return value if value in {"a", "b", "c"} else "b"


def _find_region(value):
    region_value = (value or "").strip()
    if not region_value:
        return None
    return (
        Region.objects.select_related("distributor", "manager")
        .filter(is_active=True)
        .filter(Q(code__iexact=region_value) | Q(name__iexact=region_value))
        .first()
    )


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


def _require_distributor_scope(request):
    user = _current_user(request)
    if user is None:
        return None, False, JsonResponse({"detail": "Unauthorized"}, status=401)
    if user.is_staff or user.is_superuser:
        return None, True, None
    distributor = getattr(user, "distributor_profile", None)
    if distributor is None:
        return None, False, JsonResponse({"detail": "Профиль дистрибьютора не создан в admin"}, status=403)
    return distributor, False, None


def _is_courier_user(user):
    return bool(user and (user.groups.filter(name__iexact="courier").exists() or user.is_staff or user.is_superuser))


def _require_courier_scope(request):
    user = _current_user(request)
    if user is None:
        return None, False, JsonResponse({"detail": "Unauthorized"}, status=401)
    if not _is_courier_user(user):
        return None, False, JsonResponse({"detail": "Нет доступа курьера"}, status=403)
    return user, bool(user.is_staff or user.is_superuser), None


def _is_expert_user(user):
    return bool(user and (user.groups.filter(name__iexact="expert").exists() or user.is_staff or user.is_superuser))


def _require_expert_scope(request):
    user = _current_user(request)
    if user is None:
        return None, False, JsonResponse({"detail": "Unauthorized"}, status=401)
    if not _is_expert_user(user):
        return None, False, JsonResponse({"detail": "Нет доступа эксперта"}, status=403)
    return user, bool(user.is_staff or user.is_superuser), None


def _scope_courier_tasks(user, is_admin):
    qs = CourierTask.objects.select_related("client", "assigned_courier", "order", "color_request")
    if is_admin:
        return qs
    return qs.filter(Q(assigned_courier=user) | Q(courier_id=str(user.id)) | Q(courier_id=user.username))


def _append_task_history(task, status, user=None, comment=""):
    item = {
        "status": status,
        "at": timezone.now().isoformat(),
        "by": str(user.id) if user else None,
        "comment": comment,
    }
    task.status_history = [*(task.status_history or []), item]


def _scope_clients(distributor, is_admin):
    qs = ClientProfile.objects.select_related("distributor", "manager", "user")
    return qs if is_admin else qs.filter(distributor=distributor)


def _scope_purchases(distributor, is_admin):
    qs = Purchase.objects.select_related("client", "distributor").prefetch_related("items")
    return qs if is_admin else qs.filter(distributor=distributor)


def _scope_orders(distributor, is_admin):
    qs = Order.objects.select_related("client", "store", "distributor").prefetch_related("items")
    return qs if is_admin else qs.filter(distributor=distributor)


def _scope_products(distributor, is_admin):
    qs = Product.objects.select_related("distributor")
    return qs if is_admin else qs.filter(distributor=distributor)


def _money_value(value):
    try:
        return Decimal(str(value or "0").replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _parse_items(value):
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return decoded if isinstance(decoded, list) else []


def _uploaded_file_hash(file_obj):
    if not file_obj:
        return ""
    digest = sha256()
    for chunk in file_obj.chunks():
        digest.update(chunk)
    file_obj.seek(0)
    return digest.hexdigest()


def _file_extension(name):
    lowered = (name or "").lower()
    if "." not in lowered:
        return ""
    return lowered[lowered.rfind(".") :]


def _file_type(file_obj):
    content_type = (getattr(file_obj, "content_type", "") or "").lower()
    extension = _file_extension(getattr(file_obj, "name", ""))
    if content_type.startswith("image/") or extension in {".jpg", ".jpeg", ".png", ".webp"}:
        return "image"
    if content_type == "application/pdf" or extension == ".pdf":
        return "pdf"
    if content_type.startswith("video/") or extension in {".mp4", ".mov"}:
        return "video"
    return "document"


def _upload_error(file_obj):
    extension = _file_extension(getattr(file_obj, "name", ""))
    content_type = (getattr(file_obj, "content_type", "") or "").lower()
    if extension not in ALLOWED_UPLOAD_EXTENSIONS:
        return JsonResponse({"detail": "Недопустимый тип файла", "code": "invalid_file_type"}, status=400)
    if content_type and content_type not in ALLOWED_UPLOAD_CONTENT_TYPES:
        return JsonResponse({"detail": "Недопустимый тип файла", "code": "invalid_file_type"}, status=400)
    if getattr(file_obj, "size", 0) > MAX_UPLOAD_SIZE:
        return JsonResponse({"detail": "Файл больше 10 МБ", "code": "file_too_large"}, status=400)
    return None


def _attachment_files(request):
    files = []
    seen = set()
    for field in ("attachments", "document", "file", "photo", "proof", "files"):
        for file_obj in request.FILES.getlist(field):
            marker = id(file_obj)
            if marker not in seen:
                files.append(file_obj)
                seen.add(marker)
    return files


def _create_attachments(request, related_object, files, description=""):
    content_type = ContentType.objects.get_for_model(related_object)
    uploaded_by = _current_user(request)
    attachments = []
    for file_obj in files:
        error = _upload_error(file_obj)
        if error:
            return attachments, error
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)
        attachments.append(
            Attachment.objects.create(
                file=file_obj,
                file_type=_file_type(file_obj),
                uploaded_by=uploaded_by,
                content_type=content_type,
                object_id=related_object.pk,
                description=description,
            )
        )
    return attachments, None


def _attachments_for(related_object):
    content_type = ContentType.objects.get_for_model(related_object)
    return Attachment.objects.filter(content_type=content_type, object_id=related_object.pk)


# Formatting Helpers

def _format_client(client):
    return {
        "id": str(client.id),
        "inn": client.inn,
        "name": client.company_name,
        "category": client.category,
        "region": client.region.name if client.region else "",
        "city": client.city,
        "contact": client.contact_name,
        "phone": client.phone,
        "distributorId": str(client.distributor_id),
        "managerId": str(client.manager_id) if client.manager_id else None,
        "registrationSource": client.registration_source,
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


def _format_attachment(item):
    return {
        "id": str(item.id),
        "url": item.file.url if item.file else None,
        "name": item.file.name.split("/")[-1] if item.file else "",
        "fileType": item.file_type,
        "uploadedBy": str(item.uploaded_by_id) if item.uploaded_by_id else None,
        "uploadedAt": item.uploaded_at.isoformat(),
        "description": item.description or None,
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
        "clientName": order.client.company_name,
        "clientInn": order.client.inn,
        "distributorId": str(order.distributor_id),
        "storeId": str(order.store_id),
        "storeName": order.store.name,
        "documentNumber": f"ORD-{order.id:05d}",
        "date": order.created_at.isoformat(),
        "totalAmount": float(order.total_amount),
        "status": "pending" if order.status in ("pending", "accepted") else "verified",
        "orderStatus": order.status,
        "comment": order.comment,
        "rejectionReason": order.rejection_reason or None,
        "createdAt": order.created_at.isoformat(),
        "items": items,
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


def _format_purchase(purchase):
    return {
        "id": str(purchase.id),
        "clientId": str(purchase.client_id),
        "clientName": purchase.client.company_name,
        "clientInn": purchase.client.inn,
        "distributorId": str(purchase.distributor_id),
        "documentNumber": purchase.document_number,
        "date": purchase.date.isoformat(),
        "totalAmount": float(purchase.total_amount),
        "status": purchase.status,
        "documentUrl": purchase.document_file.url if purchase.document_file else (purchase.document_url or None),
        "documentHash": purchase.document_hash or None,
        "rejectionReason": purchase.rejection_reason or None,
        "createdAt": purchase.created_at.isoformat(),
        "items": [_format_purchase_item(item) for item in purchase.items.all()],
        "attachments": [_format_attachment(item) for item in _attachments_for(purchase)],
    }


def _format_recipe_material(item):
    return {
        "id": str(item.id),
        "sku": item.sku,
        "quantity": float(item.quantity),
        "unit": item.unit,
        "comment": item.comment or None,
        "version": item.version,
    }


def _format_color_request(item):
    materials = [_format_recipe_material(m) for m in item.materials.all()]
    courier_tasks = [_format_courier_task(t) for t in item.courier_tasks.all()]
    now = timezone.now()
    is_overdue = item.sla_deadline and now > item.sla_deadline and item.status != "delivered"
    return {
        "id": str(item.id),
        "clientId": str(item.client_id),
        "carBrand": item.car_brand,
        "carModel": item.car_model,
        "carYear": item.car_year or None,
        "vin": item.vin,
        "colorCode": item.color_code,
        "colorName": item.color_name,
        "urgent": item.urgent,
        "comment": item.comment or None,
        "courierPickup": item.courier_pickup,
        "pickupAddress": item.pickup_address or None,
        "pickupDate": item.pickup_date.isoformat() if item.pickup_date else None,
        "contactPerson": item.contact_person or None,
        "contactPhone": item.contact_phone or None,
        "deliveryMethod": item.delivery_method,
        "slaDeadline": item.sla_deadline.isoformat() if item.sla_deadline else None,
        "isOverdue": is_overdue,
        "assignedDistributorId": str(item.assigned_distributor_id) if item.assigned_distributor_id else None,
        "assignedStation": item.assigned_station or None,
        "status": item.status,
        "statusHistory": item.status_history or [],
        "recipe": item.recipe or None,
        "materials": materials,
        "courierTasks": courier_tasks,
        "createdAt": item.created_at.isoformat(),
        "attachments": [_format_attachment(attachment) for attachment in _attachments_for(item)],
    }


def _format_courier_task(item):
    if not item:
        return None
    return {
        "id": str(item.id),
        "clientId": str(item.client_id),
        "clientName": item.client.company_name,
        "clientInn": item.client.inn,
        "orderId": str(item.order_id) if item.order_id else None,
        "colorRequestId": str(item.color_request_id) if item.color_request_id else None,
        "taskType": item.task_type,
        "typeDisplay": item.get_task_type_display(),
        "address": item.address,
        "timeSlot": item.time_slot,
        "status": item.status,
        "statusDisplay": item.get_status_display(),
        "assignedCourierId": str(item.courier_id) if item.courier_id else None,
        "photoProof": item.proof_photo.url if item.proof_photo else None,
        "comment": item.comment,
        "courierComment": item.courier_comment,
        "statusHistory": item.status_history or [],
        "createdAt": item.created_at.isoformat(),
        "attachments": [_format_attachment(attachment) for attachment in _attachments_for(item)],
    }


def _format_ticket(item):
    return {
        "id": str(item.id),
        "clientId": str(item.client_id),
        "clientName": item.client.company_name,
        "question": item.question,
        "category": item.category,
        "risk": item.risk,
        "status": item.status,
        "aiDraftAnswer": item.ai_draft_answer or None,
        "aiAnswer": item.ai_answer or None,
        "expertAnswer": item.expert_answer or None,
        "linkedKnowledgeCardId": str(item.linked_knowledge_card_id) if item.linked_knowledge_card_id else None,
        "similarCases": item.similar_cases or [],
        "createdAt": item.created_at.isoformat(),
        "updatedAt": item.updated_at.isoformat(),
        "attachments": [_format_attachment(a) for attachment in _attachments_for(item)],
    }


def _format_knowledge_card(item):
    return {
        "id": str(item.id),
        "title": item.title or item.problem,
        "category": item.category,
        "problem": item.problem,
        "causes": item.causes or None,
        "solution": item.solution,
        "skus": item.skus or [],
        "restrictions": item.restrictions or None,
        "status": item.status,
        "isApproved": item.status == "approved",
        "createdBy": str(item.created_by_id) if item.created_by_id else None,
        "approvedBy": str(item.approved_by_id) if item.approved_by_id else None,
        "revisionHistory": item.revision_history or [],
        "createdAt": item.created_at.isoformat(),
        "updatedAt": item.updated_at.isoformat(),
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


# Autoservice Views

@require_GET
def health(_request):
    return JsonResponse({"status": "ok", "service": "autoterra-api"})


@csrf_exempt
@require_POST
def register(request):
    payload = _json(request)
    print(f"DEBUG: Register payload: {payload}")
    serializer = RegistrationSerializer(payload)
    if not serializer.is_valid():
        print(f"DEBUG: Serializer errors: {serializer.errors}")
        # Возвращаем detail для фронтенда, чтобы он мог показать ошибку
        first_err_msg = "Ошибка валидации"
        if serializer.errors:
            # Извлекаем первое сообщение об ошибке
            field = next(iter(serializer.errors))
            first_err_msg = f"{field}: {serializer.errors[field]}"
            if isinstance(serializer.errors[field], list):
                first_err_msg = serializer.errors[field][0]
            else:
                first_err_msg = str(serializer.errors[field])
        
        return JsonResponse({"detail": first_err_msg, "errors": serializer.errors}, status=400)
    
    validated_data = serializer.validated_data
    username = validated_data['username']
    password = validated_data['password']
    inn = validated_data['inn']
    region = validated_data['region']
    company_name = validated_data['company_name']
    contact_name = validated_data['contact_name']
    
    # 1. Проверка уникальности номера телефона
    if User.objects.filter(username=username).exists():
        return JsonResponse({
            "detail": f"Номер телефона {username} уже используется. Пожалуйста, войдите в аккаунт или используйте другой номер.", 
            "code": "phone_duplicate"
        }, status=409)

    # 2. Проверка уникальности ИНН (локально в текущем регионе)
    if ClientProfile.objects.filter(inn=inn, region=region).exists():
        return JsonResponse({
            "detail": f"Организация с ИНН {inn} уже зарегистрирована в регионе {region.name}. Повторная регистрация в одном и том же регионе запрещена.", 
            "code": "inn_duplicate"
        }, status=409)

    # Если ИНН есть в других регионах, помечаем как филиал (статус "на проверке")
    is_branch = ClientProfile.objects.filter(inn=inn).exclude(region=region).exists()
    status = "under_review" if is_branch else "new"

    try:
        with transaction.atomic():
            # Создаем пользователя
            user = User.objects.create_user(username=username, password=password)
            
            # Создаем профиль
            client = ClientProfile.objects.create(
                user=user,
                inn=inn,
                company_name=company_name,
                region=region,
                status=status,
                phone=username,
                city=region.name,
                contact_name=contact_name,
                registration_source="app",
                distributor=region.distributor,
                manager=region.manager
            )
            
            token = secrets.token_hex(24)
            AuthToken.objects.create(key=token, user=user)
    except IntegrityError as e:
        # Резервный обработчик на случай гонки условий
        return JsonResponse({
            "detail": "Ошибка уникальности данных: возможно, ИНН или телефон были зарегистрированы только что другим пользователем.",
            "code": "integrity_error"
        }, status=409)
    except Exception as e:
        return JsonResponse({
            "detail": f"Внутренняя ошибка сервера при регистрации: {str(e)}",
            "code": "server_error"
        }, status=500)

    return JsonResponse({
        "status": "success",
        "token": token,
        "client": _format_client(client),
        "requires_approval": status == "under_review"
    }, status=201)


@csrf_exempt
@require_POST
def login(request):
    payload = _json(request)
    phone = (payload.get("phone") or "").strip()
    password = payload.get("password") or ""
    normalized = _normalize_phone(phone)

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
            if _is_courier_user(user):
                return JsonResponse({"token": token, "user": {"id": str(user.id), "phone": user.username, "role": "courier", "status": "active"}})
            if _is_expert_user(user):
                return JsonResponse({"token": token, "user": {"id": str(user.id), "phone": user.username, "role": "expert", "status": "active"}})
            return JsonResponse({"detail": "Профиль клиента или дистрибьютора не найден"}, status=403)
        return JsonResponse({"token": token, "user": {"id": str(user.id), "phone": user.username, "role": "distributor", "status": "active", "distributor": _format_distributor(distributor)}})

    return JsonResponse({"token": token, "user": {"id": str(user.id), "phone": client.phone, "role": "autoservice", "status": client.status}})


@require_GET
def me(request):
    client, err = _require_client(request)
    if err:
        return err
    return JsonResponse({"id": str(client.user_id), "phone": client.phone, "role": "autoservice", "client": _format_client(client), "distributor": _format_distributor(client.distributor)})


@require_GET
def dashboard(request):
    client, err = _require_client(request)
    if err:
        return err
    purchases = client.purchases.prefetch_related("items").order_by("-date")[:2]
    color_requests = client.color_requests.exclude(status="delivered").order_by("-created_at")[:2]
    return JsonResponse({"client": _format_client(client), "distributor": _format_distributor(client.distributor), "unreadCount": client.notifications.filter(is_read=False).count(), "recentPurchases": [_format_purchase(item) for item in purchases], "activeColorRequests": [_format_color_request(item) for item in color_requests]})


@require_GET
def stores(request):
    client, err = _require_client(request)
    if err:
        return err
    qs = client.stores.filter(is_active=True)
    return JsonResponse({"results": [_format_store(item) for item in qs]})


@require_GET
def products(request):
    client, err = _require_client(request)
    if err:
        return err
    qs = Product.objects.filter(distributor=client.distributor, is_active=True)
    category = (request.GET.get("category") or "").strip()
    search = (request.GET.get("search") or "").strip()
    if category:
        qs = qs.filter(category=category)
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(sku__icontains=search) | Q(brand__icontains=search))
    return JsonResponse({"categories": list(Product.objects.filter(distributor=client.distributor, is_active=True).values_list("category", flat=True).distinct()), "results": [_format_product(item) for item in qs.order_by("category", "name")]})


@require_GET
def order_config(request):
    client, err = _require_client(request)
    if err:
        return err
    stores_qs = client.stores.filter(is_active=True)
    products_qs = Product.objects.filter(distributor=client.distributor, is_active=True)
    categories = list(products_qs.values_list("category", flat=True).distinct())
    return JsonResponse({
        "client": _format_client(client),
        "distributor": _format_distributor(client.distributor),
        "stores": [_format_store(s) for s in stores_qs],
        "categories": categories,
        "products": [_format_product(p) for p in products_qs],
    })


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
    store = Store.objects.filter(id=store_id, client=client, is_active=True).first()
    if store is None or not items:
        return JsonResponse({"detail": "Выберите магазин и товары"}, status=400)
    with transaction.atomic():
        order = Order.objects.create(client=client, store=store, distributor=client.distributor, comment=(payload.get("comment") or "").strip())
        for raw in items:
            product = Product.objects.filter(id=raw.get("productId"), distributor=client.distributor, is_active=True).first()
            if product:
                OrderItem.objects.create(order=order, product=product, sku=product.sku, name=product.name, category=product.category, brand=product.brand, volume=product.volume, price=product.price, quantity=int(raw.get("quantity") or 1))
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
    serializer = PurchaseSerializer(payload)
    if not serializer.is_valid():
        return JsonResponse({"errors": serializer.errors}, status=400)
    
    validated_data = serializer.validated_data
    
    existing = Purchase.objects.filter(
        client=client,
        document_number=validated_data['document_number'],
        date=validated_data['date'],
        total_amount=validated_data['amount']
    ).first()
    
    if existing:
        return JsonResponse({
            "error": "duplicate_detected",
            "message": f"Покупка с такими данными уже загружена и имеет статус [{existing.get_status_display()}]"
        }, status=400)
    
    with transaction.atomic():
        purchase = Purchase.objects.create(
            client=client,
            distributor=client.distributor,
            document_number=validated_data['document_number'],
            date=validated_data['date'],
            total_amount=validated_data['amount'],
            status="new",
        )
        items = _parse_items(payload.get("items"))
        for raw in items:
            PurchaseItem.objects.create(
                purchase=purchase, 
                sku=raw.get("sku"), 
                name=raw.get("name"), 
                quantity=int(raw.get("quantity") or 1), 
                price=_money_value(raw.get("price"))
            )
            
    return JsonResponse({"purchase": _format_purchase(purchase)}, status=201)


# Color Lab Views

@require_GET
def color_requests(request):
    client, err = _require_client(request)
    if err:
        return err
    qs = client.color_requests.prefetch_related("materials", "courier_tasks").all()
    return JsonResponse({"results": [_format_color_request(item) for item in qs]})


def _append_color_history(item, status, user=None, comment=""):
    history_item = {
        "status": status,
        "at": timezone.now().isoformat(),
        "by": str(user.id) if user else None,
        "comment": comment,
    }
    item.status_history = [*(item.status_history or []), history_item]


@csrf_exempt
@require_POST
def create_color_request(request):
    client, err = _require_client(request)
    if err:
        return err
    payload = request.POST if (request.content_type or "").startswith("multipart/form-data") else _json(request)
    files = _attachment_files(request)
    with transaction.atomic():
        item = ColorRequest.objects.create(
            client=client,
            car_brand=(payload.get("carBrand") or "").strip(),
            car_model=(payload.get("carModel") or "").strip(),
            car_year=(payload.get("carYear") or "").strip()[:4],
            vin=(payload.get("vin") or "").strip(),
            color_code=(payload.get("colorCode") or "").strip(),
            color_name=(payload.get("colorName") or "").strip(),
            urgent=str(payload.get("urgent")).lower() in {"true", "1", "yes"},
            comment=(payload.get("comment") or "").strip(),
            courier_pickup=str(payload.get("courierPickup")).lower() in {"true", "1", "yes"},
            pickup_address=(payload.get("pickupAddress") or payload.get("address") or client.city).strip(),
            pickup_date=_dt(payload.get("pickupDate") or payload.get("pickupTime") or payload.get("scheduledTime")) if payload.get("pickupDate") or payload.get("pickupTime") or payload.get("scheduledTime") else None,
            contact_person=(payload.get("contactPerson") or payload.get("contactName") or client.contact_name).strip(),
            contact_phone=(payload.get("contactPhone") or client.phone).strip(),
            delivery_method=(payload.get("deliveryMethod") or "courier").strip(),
            assigned_distributor=client.distributor,
        )
        sla_hours = 4 if item.urgent else 24
        item.sla_deadline = item.created_at + timezone.timedelta(hours=sla_hours)
        _append_color_history(item, "created", _current_user(request), "Заявка создана")
        item.save(update_fields=["sla_deadline", "status_history"])
        _create_attachments(request, item, files, description="Фото для Color Lab")
        if item.courier_pickup:
            task = CourierTask.objects.create(
                client=client,
                color_request=item,
                type="pickup",
                address=item.pickup_address or client.city,
                scheduled_time=item.pickup_date or (timezone.now() + timezone.timedelta(hours=2)),
                contact_name=item.contact_person,
                contact_phone=item.contact_phone,
                car_description=f"{item.car_brand} {item.car_model} · {item.color_code}",
                comment=(item.comment or "Забор лючка для Color Lab").strip(),
            )
            _append_task_history(task, "created", _current_user(request), "Создано из Color Lab")
            task.save(update_fields=["status_history"])
    return JsonResponse({"request": _format_color_request(item)}, status=201)


# Courier Views

@require_GET
def courier_tasks(request):
    client, err = _require_client(request)
    if err:
        return err
    qs = client.courier_tasks.all()
    return JsonResponse({"results": [_format_courier_task(item) for item in qs]})


@csrf_exempt
@require_POST
def create_courier_task(request):
    client, err = _require_client(request)
    if err:
        return err
    payload = _json(request)
    task = CourierTask.objects.create(
        client=client,
        type=payload.get("type", "delivery"),
        address=payload.get("address", client.city),
        scheduled_time=_dt(payload.get("scheduledTime")),
        contact_name=payload.get("contactName", client.contact_name),
        contact_phone=payload.get("contactPhone", client.phone),
        car_description=payload.get("carDescription", ""),
        comment=payload.get("comment", ""),
    )
    _append_task_history(task, "created", _current_user(request), "Создано клиентом")
    task.save(update_fields=["status_history"])
    return JsonResponse({"task": _format_courier_task(task)}, status=201)


@csrf_exempt
@require_POST
def courier_task_proof(request, task_id):
    client, err = _require_client(request)
    if err:
        return err
    task = client.courier_tasks.filter(id=task_id).first()
    if task:
        files = _attachment_files(request)
        _create_attachments(request, task, files, description="Фото подтверждение")
    return JsonResponse({"task": _format_courier_task(task)})


@require_GET
def courier_my_tasks(request):
    user, is_admin, err = _require_courier_scope(request)
    if err:
        return err
    
    # Задачи, назначенные на текущего курьера
    qs = CourierTask.objects.filter(courier=user).order_by("-created_at")
    if is_admin:
        qs = CourierTask.objects.all().order_by("-created_at")
        
    return JsonResponse({"results": [_format_courier_task(item) for item in qs]})

@csrf_exempt
@require_http_methods(["PATCH", "POST"])
def courier_update_task_status(request, task_id):
    user, is_admin, err = _require_courier_scope(request)
    if err:
        return err
    
    task = CourierTask.objects.filter(id=task_id).first()
    if not task:
        return JsonResponse({"detail": "Задача не найдена"}, status=404)
    
    if not is_admin and task.courier != user:
        return JsonResponse({"detail": "Нет прав для редактирования этой задачи"}, status=403)

    # Приоритетно берем из POST (для multipart), затем из JSON
    status = request.POST.get("status")
    comment = request.POST.get("courier_comment")

    if not status:
        payload = _json(request)
        status = payload.get("status")
        if not comment:
            comment = payload.get("courier_comment")

    if status:
        task.status = status
        _append_task_history(task, status, user)
    
    if comment is not None:
        task.courier_comment = comment

    # Обработка фото-подтверждения
    if "proof_photo" in request.FILES:
        task.proof_photo = request.FILES["proof_photo"]

    task.save()
    return JsonResponse({"task": _format_courier_task(task)})


@csrf_exempt
@require_POST
def assign_courier_task(request, task_id):
    user, is_admin, err = _require_courier_scope(request)
    if not is_admin:
        return JsonResponse({"detail": "Только администратор может назначать курьеров"}, status=403)
    courier_id = _json(request).get("courierId")
    task = CourierTask.objects.filter(id=task_id).first()
    if task:
        task.assigned_courier_id = courier_id
        task.status = "assigned"
        _append_task_history(task, "assigned", user, f"Назначен курьер {courier_id}")
        task.save(update_fields=["assigned_courier", "status", "status_history"])
    return JsonResponse({"task": _format_courier_task(task)})


# Distributor Views

@require_GET
def distributor_dashboard(request):
    distributor, is_admin, err = _require_distributor_scope(request)
    if err:
        return err
    clients = _scope_clients(distributor, is_admin)
    purchases = _scope_purchases(distributor, is_admin)
    orders_qs = _scope_orders(distributor, is_admin)
    return JsonResponse({"metrics": {"clients": clients.count(), "purchasesToVerify": purchases.filter(status__in=["pending", "pending_verification"]).count(), "ordersToProcess": orders_qs.filter(status="pending").count()}})


@require_GET
def distributor_clients(request):
    distributor, is_admin, err = _require_distributor_scope(request)
    if err:
        return err
    qs = _scope_clients(distributor, is_admin)
    return JsonResponse({"results": [_format_client(item) for item in qs]})


@require_GET
def distributor_purchases(request):
    distributor, is_admin, err = _require_distributor_scope(request)
    if err:
        return err
    
    # Список покупок, ожидающих проверки (или всех покупок для этого дистрибьютора)
    qs = _scope_purchases(distributor, is_admin).order_by("-date")
    
    # Фильтрация по статусу (например, только новые и на проверке)
    status_filter = request.GET.get("status")
    if status_filter:
        qs = qs.filter(status=status_filter)
    else:
        qs = qs.filter(status__in=["new", "pending", "pending_verification", "under_review"])

    return JsonResponse({"results": [_format_purchase(item) for item in qs]})


@csrf_exempt
@require_http_methods(["PATCH", "POST"])
def distributor_verify_purchase(request, purchase_id):
    distributor, is_admin, err = _require_distributor_scope(request)
    if err:
        return err
    
    purchase = _scope_purchases(distributor, is_admin).filter(id=purchase_id).first()
    if not purchase:
        return JsonResponse({"detail": "Покупка не найдена"}, status=404)

    payload = _json(request)
    status = payload.get("status")
    reason = payload.get("rejection_reason")

    if status not in ["verified", "rejected"]:
        return JsonResponse({"detail": "Некорректный статус. Используйте verified или rejected"}, status=400)

    purchase.status = status
    if status == "rejected" and reason:
        purchase.rejection_reason = reason
    
    purchase.save()
    return JsonResponse({"purchase": _format_purchase(purchase)})


@require_GET
def distributor_orders(request):
    distributor, is_admin, err = _require_distributor_scope(request)
    if err:
        return err
    
    qs = _scope_orders(distributor, is_admin).order_by("-created_at")
    
    status_filter = request.GET.get("status")
    if status_filter:
        qs = qs.filter(status=status_filter)

    return JsonResponse({"results": [_format_order(item) for item in qs]})


@csrf_exempt
@require_http_methods(["PATCH", "POST"])
def distributor_update_order_status(request, order_id):
    distributor, is_admin, err = _require_distributor_scope(request)
    if err:
        return err
    
    order = _scope_orders(distributor, is_admin).filter(id=order_id).first()
    if not order:
        return JsonResponse({"detail": "Заказ не найден"}, status=404)

    payload = _json(request)
    status = payload.get("status")
    reason = payload.get("rejection_reason")

    if status not in ["accepted", "rejected", "done"]:
        return JsonResponse({"detail": "Некорректный статус"}, status=400)

    order.status = status
    if status == "rejected" and reason:
        order.rejection_reason = reason
    
    order.save()
    return JsonResponse({"order": _format_order(order)})


@require_GET
def distributor_stock(request):
    distributor, is_admin, err = _require_distributor_scope(request)
    if err:
        return err
    qs = _scope_products(distributor, is_admin)
    return JsonResponse({"results": [_format_product(item) for item in qs]})


@csrf_exempt
@require_POST
def distributor_stock_upload(request):
    distributor, is_admin, err = _require_distributor_scope(request)
    if err:
        return err
    if is_admin:
        return JsonResponse({"detail": "Admin must specify distributorId"}, status=400)
    items = _json(request).get("items", [])
    with transaction.atomic():
        for raw in items:
            Product.objects.update_or_create(
                distributor=distributor,
                sku=raw.get("sku"),
                defaults={
                    "name": raw.get("name"),
                    "category": raw.get("category"),
                    "brand": raw.get("brand", "AutoTerra"),
                    "price": _money_value(raw.get("price")),
                    "quantity": int(raw.get("quantity", 0)),
                    "status": raw.get("status", "inStock"),
                }
            )
    return JsonResponse({"status": "ok", "processed": len(items)})


@require_GET
def distributor_reports(request):
    distributor, is_admin, err = _require_distributor_scope(request)
    if err:
        return err
    return JsonResponse({"summary": "Stock and order reports will be here"})


# Referral Views

@require_GET
def referrals(request):
    client, err = _require_client(request)
    if err:
        return err
    qs = client.referrals.all()
    stats = {
        "invitedCount": qs.count(),
        "registeredCount": qs.filter(is_registered=True).count(),
        "buyersCount": qs.filter(has_purchase=True).count(),
        "giftCount": qs.filter(condition_met=True).count(),
        "purchaseAmount": float(sum(r.purchase_amount for r in qs)),
    }
    return JsonResponse({"stats": stats, "results": [_format_referral(r) for r in qs]})


@csrf_exempt
@require_POST
def create_referral(request):
    client, err = _require_client(request)
    if err:
        return err
    payload = _json(request)
    item = Referral.objects.create(
        inviter=client,
        invitee_inn=payload.get("inviteeInn"),
        invitee_name=payload.get("inviteeName"),
        region=payload.get("region", client.region),
    )
    return JsonResponse({"referral": _format_referral(item)}, status=201)


# Support & Expert Views

@require_GET
def tickets(request):
    client, err = _require_client(request)
    if err:
        # Check if it's an expert
        user, is_admin, expert_err = _require_expert_scope(request)
        if expert_err:
            return err # Original unauthorized error
        # It's an expert, return all tickets or filtered
        qs = ExpertTicket.objects.select_related("client").prefetch_related("materials").all()
        return JsonResponse({"results": [_format_ticket(item) for item in qs]})
    
    # It's a client, return only their tickets
    return JsonResponse({"results": [_format_ticket(item) for item in client.expert_tickets.all()]})


@csrf_exempt
@require_POST
def create_ticket(request):
    client, err = _require_client(request)
    if err:
        return err
    payload = request.POST if (request.content_type or "").startswith("multipart/form-data") else _json(request)
    files = _attachment_files(request)
    with transaction.atomic():
        item = ExpertTicket.objects.create(
            client=client,
            question=(payload.get("question") or "").strip(),
            category=(payload.get("category") or "General").strip(),
            risk=payload.get("risk", "low"),
            status="open",
        )
        _create_attachments(request, item, files, description="Вложения к тикету")
        
        # Simple AI draft generation logic
        cards = KnowledgeCard.objects.filter(status="approved").filter(
            Q(problem__icontains=item.question) | Q(title__icontains=item.question)
        )
        if cards.exists():
            card = cards.first()
            item.ai_draft_answer = f"Предположительный ответ на основе базы знаний:\n{card.solution}"
            item.similar_cases = [str(card.id)]
            item.save(update_fields=["ai_draft_answer", "similar_cases"])

    return JsonResponse({"ticket": _format_ticket(item)}, status=201)


@csrf_exempt
@require_POST
def expert_answer_ticket(request, ticket_id):
    user, is_admin, err = _require_expert_scope(request)
    if err:
        return err
    payload = _json(request)
    answer = (payload.get("answer") or "").strip()
    status = payload.get("status", "expertAnswered")
    
    ticket = ExpertTicket.objects.filter(id=ticket_id).first()
    if not ticket:
        return JsonResponse({"detail": "Тикет не найден"}, status=404)
        
    with transaction.atomic():
        ticket.expert_answer = answer
        ticket.status = status
        ticket.save(update_fields=["expert_answer", "status"])
        
        # If expert wants to create a knowledge card from this
        if payload.get("createKnowledgeCard"):
            card = KnowledgeCard.objects.create(
                title=f"Кейс: {ticket.category}",
                category=ticket.category,
                problem=ticket.question,
                solution=answer,
                status="draft",
                created_by=user,
            )
            ticket.linked_knowledge_card = card
            ticket.save(update_fields=["linked_knowledge_card"])
            
    return JsonResponse({"ticket": _format_ticket(ticket)})


@require_GET
def knowledge_cards(request):
    user = _current_user(request)
    # Check if user is expert to see drafts
    if _is_expert_user(user):
        qs = KnowledgeCard.objects.all()
    else:
        qs = KnowledgeCard.objects.filter(status="approved")
    return JsonResponse({"results": [_format_knowledge_card(item) for item in qs]})


@csrf_exempt
@require_POST
def update_knowledge_card(request, card_id):
    user, is_admin, err = _require_expert_scope(request)
    if err:
        return err
    payload = _json(request)
    card = KnowledgeCard.objects.filter(id=card_id).first()
    if not card:
        return JsonResponse({"detail": "Карточка не найдена"}, status=404)
        
    fields = ["title", "category", "problem", "causes", "solution", "status", "skus", "restrictions"]
    updated_fields = []
    for f in fields:
        if f in payload:
            setattr(card, f, payload[f])
            updated_fields.append(f)
            
    if payload.get("status") == "approved":
        card.approved_by = user
        updated_fields.append("approved_by")
        
    card.save(update_fields=updated_fields)
    return JsonResponse({"card": _format_knowledge_card(card)})


@require_GET
def regions(request):
    qs = Region.objects.filter(is_active=True).order_by("name")
    return JsonResponse({
        "results": [
            {
                "id": str(item.id),
                "code": item.code,
                "name": item.name,
                "active": item.is_active,
            }
            for item in qs
        ]
    })


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


def _normalize_words(text):
    import re
    if not text:
        return set()
    text = text.lower()
    return set(re.findall(r"[a-zа-я0-9]+", text))


def _score_card(query_words, card):
    if not query_words:
        return 0.0
    
    weights = {
        "problem": 1.2,
        "title": 1.0,
        "category": 0.5,
        "solution": 0.3,
    }
    
    score = 0.0
    for field, weight in weights.items():
        field_val = getattr(card, field, "")
        if isinstance(field_val, list):
            field_val = " ".join(field_val)
        
        field_words = _normalize_words(field_val)
        if not field_words:
            continue
            
        common = query_words.intersection(field_words)
        if common:
            # Bonus for matching more query words
            coverage = len(common) / len(query_words)
            score += coverage * weight
            
            # Exact match bonus for short fields
            if len(query_words) == len(field_words) and coverage == 1.0:
                score += 0.5 * weight

    return score


@csrf_exempt
@require_POST
def ai_chat(request):
    client, err = _require_client(request)
    if err:
        return err
    payload = _json(request)
    question = (payload.get("message") or "").strip()
    
    query_words = _normalize_words(question)
    if not query_words:
        return JsonResponse({"answer": "Пожалуйста, введите ваш вопрос.", "suggestEscalation": False})

    # 1. Fetch approved cards
    cards = KnowledgeCard.objects.filter(status="approved")
    
    # 2. Ranking
    ranked = []
    for card in cards:
        score = _score_card(query_words, card)
        if score > 0.1: # Minimum threshold to even consider
            ranked.append((score, card))
    
    ranked.sort(key=lambda x: x[0], reverse=True)
    
    # 3. Decision making
    CONFIDENCE_THRESHOLD = 0.5
    best_score = ranked[0][0] if ranked else 0
    card = ranked[0][1] if ranked else None

    if card and best_score >= CONFIDENCE_THRESHOLD:
        # Guardrails (Dangerous keywords check)
        dangerous_keywords = ["пропорции", "гарантия", "совместимость", "срок годности"]
        looks_dangerous = any(kw in question.lower() for kw in dangerous_keywords)
        
        solution_lower = card.solution.lower()
        if looks_dangerous and not any(kw in solution_lower for kw in dangerous_keywords):
            answer = (f"Я нашёл статью '{card.title}', но в ней нет точной информации по вашим критериям "
                      "(пропорции/совместимость). Чтобы не рисковать качеством ремонта, я создал "
                      "заявку для нашего технолога.")
            source_id = str(card.id)
            _auto_create_ticket(client, question, "AI - High Risk Match")
            return JsonResponse({"answer": answer, "sourceId": source_id, "suggestEscalation": True})
        
        answer = f"На основе базы знаний ({card.category}):\n\n{card.solution}"
        if card.restrictions:
            answer += f"\n\nВажно: {card.restrictions}"
        
        return JsonResponse({
            "answer": answer,
            "sourceId": str(card.id),
            "suggestEscalation": False
        })

    # 4. Low confidence: Automatic Escalation
    _auto_create_ticket(client, question, "AI - Low Confidence")
    return JsonResponse({
        "answer": ("К сожалению, в базе знаний нет точного ответа на ваш вопрос. "
                  "Я автоматически создал обращение к нашему технологу. Он ответит вам в ближайшее время."),
        "sourceId": None,
        "suggestEscalation": True
    })


def _auto_create_ticket(client, question, category):
    # Check for existing open ticket with same question to avoid spam
    exists = ExpertTicket.objects.filter(
        client=client, 
        question=question, 
        status__in=["open", "escalated"]
    ).exists()
    
    if not exists:
        ExpertTicket.objects.create(
            client=client,
            question=question,
            category=category,
            status="open",
            risk="medium"
        )
