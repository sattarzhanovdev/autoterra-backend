"""One validated catalog write path for ERP push and pull strategies."""
from decimal import Decimal
from django.db import transaction
from django.db.models import Count, Max, Q
from django.utils import timezone
from api.models import Distributor, Product, SyncLog, normalize_product_images
from api.serializers import coerce_decimal


def catalog_revision(distributor):
    state = Product.objects.filter(distributor=distributor).aggregate(
        latest=Max("updated_at"), count=Count("pk"), active=Count("pk", filter=Q(is_active=True)))
    return f"{state['latest'].isoformat() if state['latest'] else 'empty'}:{state['count']}:{state['active']}"


def sync_catalog(distributor, items, *, full_sync=False, allow_create=True, sync_type="catalog_sync"):
    started = timezone.now()
    result = {"created": 0, "updated": 0, "deactivated": 0, "errors": [],
              "startedAt": started.isoformat(), "distributorId": distributor.pk, "syncType": "full" if full_sync else "partial"}
    seen = set()
    aliases = {"externalId": "external_id", "isActive": "is_active", "videoUrl": "video_url", "wbArticle": "wb_article", "groupName": "group_name", "packageHeight": "package_height", "packageLength": "package_length", "packageWidth": "package_width", "vatRate": "vat_rate"}
    text_fields = {"sku", "external_id", "name", "description", "category", "brand", "video_url", "color", "barcode", "wb_article", "group_name", "tnved", "vat_rate"}
    with transaction.atomic():
        Distributor.objects.select_for_update().get(pk=distributor.pk)
        for index, raw in enumerate(items, 1):
            try:
                with transaction.atomic():
                    if not isinstance(raw, dict):
                        raise ValueError("Ожидается объект товара")
                    item = {aliases.get(k, k): v for k, v in raw.items()}
                    sku, external = item.get("sku"), item.get("external_id")
                    if not (sku or external):
                        raise ValueError("Нужен sku или external_id")
                    qs = Product.objects.filter(distributor=distributor)
                    product = qs.filter(external_id=external).first() if external else None
                    product = product or (qs.filter(sku=sku).first() if sku else None)
                    new = product is None
                    if new and not allow_create:
                        raise ValueError("Product not found")
                    product = product or Product(distributor=distributor, sku=sku or external, name="Новый товар", category="Без категории", quantity=0, status="outOfStock", price=Decimal(0))
                    for key in text_fields & item.keys():
                        if item[key] is None and key != "external_id":
                            raise ValueError(f"Некорректное поле {key}")
                        setattr(product, key, str(item[key]).strip() if item[key] is not None else None)
                    for field in ("price", "volume", "weight", "package_height", "package_length", "package_width"):
                        if field in item:
                            model_field = Product._meta.get_field(field)
                            value = coerce_decimal(item[field], model_field.max_digits, model_field.decimal_places)
                            if value is None or value < 0:
                                raise ValueError(f"Некорректное поле {field}")
                            setattr(product, field, value)
                    if "quantity" in item:
                        qty = item["quantity"]
                        if isinstance(qty, bool) or str(qty) != str(int(qty)) or not 0 <= int(qty) <= 2147483647:
                            raise ValueError("Некорректный остаток")
                        product.quantity = int(qty)
                        if "status" not in item:
                            product.status = "inStock" if product.quantity > 5 else "low" if product.quantity else "outOfStock"
                    if "status" in item:
                        if item["status"] not in dict(Product.STOCK_CHOICES):
                            raise ValueError("Некорректный статус")
                        product.status = item["status"]
                    if "is_active" in item:
                        if not isinstance(item["is_active"], bool):
                            raise ValueError("is_active должен быть boolean")
                        product.is_active = item["is_active"]
                    elif full_sync:
                        product.is_active = True
                    for field in ("images", "synonyms"):
                        if field in item:
                            setattr(product, field, normalize_product_images(item[field]) if field == "images" else item[field])
                    product.full_clean(exclude=["search_text"])
                    product.save()
                    seen.add(product.pk)
                    result["created" if new else "updated"] += 1
            except Exception as exc:
                result["errors"].append({"row": index, "message": str(exc)})
        # An incomplete snapshot cannot authorize removing omitted products.
        if full_sync and not result["errors"]:
            result["deactivated"] = Product.objects.filter(distributor=distributor, is_active=True).exclude(pk__in=seen).update(is_active=False)
        result["deactivationSkipped"] = bool(full_sync and result["errors"])
        result["finishedAt"] = timezone.now().isoformat()
        result["status"] = "partial" if result["errors"] else "success"
        SyncLog.objects.create(distributor=distributor, sync_type=sync_type, status="error" if result["errors"] else "success", details=result)
    return result
