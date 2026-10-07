"""Shared product substring search with optional Elasticsearch ranking.

Membership is always determined by normalized database data, so pagination,
distributor isolation and partial synonym matching do not depend on ES refresh.
Prices and stock continue to be read from the supplied QuerySet.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from django.conf import settings
from django.db.models import Case, When

logger = logging.getLogger(__name__)

#: Поля, по которым ищем, и их вес в Elasticsearch.
_SEARCH_FIELDS = ["name^3", "synonyms^3", "sku^2", "brand", "category"]

#: ES ranks up to this many hits; remaining database matches stay visible.
_MAX_HITS = 1000


def is_elasticsearch_enabled() -> bool:
    return bool(getattr(settings, "ELASTICSEARCH_URL", ""))


def index_name() -> str:
    return getattr(settings, "ELASTICSEARCH_INDEX", "autoterra-products")


def search_products(queryset, query: str):
    """Сужает ``queryset`` до товаров, подходящих под ``query``.

    Пустой запрос возвращает queryset без изменений. Если Elasticsearch
    настроен, но недоступен, откатываемся на поиск в БД: выдача товаров важнее,
    чем строгое использование ES.
    """
    from api.services.product_names import normalize_search_text
    query = normalize_search_text(query)
    if not query:
        return queryset
    matches = _database_search(queryset, query)
    # The database defines membership, including partial matches and all tenants.
    # ES is an optional ranking aid; stale/limited hits cannot hide matches.
    if is_elasticsearch_enabled():
        try:
            ids = _elasticsearch_ids(query)
            if ids:
                ordering = Case(*[When(id=pk, then=position) for position, pk in enumerate(ids)], default=len(ids))
                return matches.order_by(ordering, "category", "name", "pk")
        except Exception as exc:
            logger.warning("Elasticsearch недоступен, откат на поиск в БД: %s", exc)
    return matches


def _database_search(queryset, query: str):
    return queryset.filter(search_text__contains=query)


def _elasticsearch_ids(query: str):
    """Возвращает id товаров в порядке релевантности либо ``None``.

    ``None`` означает «ответ непригоден» — вызывающий код откатится на БД.
    """
    body = {
        "size": _MAX_HITS,
        "_source": False,
        "query": {
            "multi_match": {
                "query": query,
                "fields": _SEARCH_FIELDS,
                # Опечатки в артикулах и названиях — обычное дело у операторов.
                "fuzziness": "AUTO",
                "operator": "and",
            }
        },
    }
    payload = _request(f"/{index_name()}/_search", body)
    hits = (payload or {}).get("hits", {}).get("hits")
    if hits is None:
        return None

    ids = []
    for hit in hits:
        try:
            ids.append(int(hit["_id"]))
        except (KeyError, TypeError, ValueError):
            continue
    return ids


def _request(path: str, body: dict | None = None, method: str = "POST"):
    url = settings.ELASTICSEARCH_URL.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    timeout = getattr(settings, "ELASTICSEARCH_TIMEOUT", 3)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


# ── Индексация ───────────────────────────────────────────────────────────────

def product_document(product) -> dict:
    return {
        "sku": product.sku,
        "name": product.name,
        "synonyms": product.synonyms,
        "category": product.category,
        "brand": product.brand,
        "description": product.description or "",
        "distributorId": product.distributor_id,
        "isActive": product.is_active,
    }


def index_product(product) -> bool:
    """Кладёт один товар в индекс. ``False`` — если ES выключен или недоступен."""
    if not is_elasticsearch_enabled():
        return False
    try:
        _request(f"/{index_name()}/_doc/{product.id}", product_document(product), method="PUT")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось проиндексировать товар %s: %s", product.id, exc)
        return False


def delete_product(product_id) -> bool:
    if not is_elasticsearch_enabled():
        return False
    try:
        _request(f"/{index_name()}/_doc/{product_id}", None, method="DELETE")
        return True
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return True  # уже нет в индексе — цель достигнута
        logger.warning("Не удалось удалить товар %s из индекса: %s", product_id, exc)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось удалить товар %s из индекса: %s", product_id, exc)
        return False


def bulk_index(products) -> int:
    """Массовая переиндексация. Возвращает число отправленных документов."""
    if not is_elasticsearch_enabled():
        return 0

    lines = []
    count = 0
    for product in products:
        lines.append(json.dumps({"index": {"_index": index_name(), "_id": product.id}}))
        lines.append(json.dumps(product_document(product)))
        count += 1
    if not count:
        return 0

    # _bulk принимает NDJSON и требует перевода строки в конце.
    payload = "\n".join(lines) + "\n"
    url = settings.ELASTICSEARCH_URL.rstrip("/") + "/_bulk"
    request = urllib.request.Request(
        url,
        data=payload.encode(),
        method="POST",
        headers={"Content-Type": "application/x-ndjson"},
    )
    timeout = getattr(settings, "ELASTICSEARCH_TIMEOUT", 3)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()
    return count


def ensure_index() -> bool:
    """Создаёт индекс с русским анализатором, если его ещё нет."""
    if not is_elasticsearch_enabled():
        return False
    try:
        _request(f"/{index_name()}", None, method="HEAD")
        return True
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("Elasticsearch недоступен: %s", exc)
        return False

    settings_body = {
        "settings": {
            "analysis": {
                "analyzer": {
                    "ru": {
                        "tokenizer": "standard",
                        "filter": ["lowercase", "russian_stop", "russian_stemmer"],
                    }
                },
                "filter": {
                    "russian_stop": {"type": "stop", "stopwords": "_russian_"},
                    "russian_stemmer": {"type": "stemmer", "language": "russian"},
                },
            }
        },
        "mappings": {
            "properties": {
                "sku": {"type": "keyword"},
                "name": {"type": "text", "analyzer": "ru"},
                "synonyms": {"type": "text", "analyzer": "ru"},
                "category": {"type": "text", "analyzer": "ru"},
                "brand": {"type": "text", "analyzer": "ru"},
                "description": {"type": "text", "analyzer": "ru"},
                "distributorId": {"type": "integer"},
                "isActive": {"type": "boolean"},
            }
        },
    }
    _request(f"/{index_name()}", settings_body, method="PUT")
    return True
