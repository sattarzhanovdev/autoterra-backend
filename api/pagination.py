"""Единая пагинация для всех списочных эндпоинтов API.

Формат ответа обратно совместим со старым: ключ ``results`` остаётся списком
элементов текущей страницы. Дополнительно отдаётся блок ``pagination`` с
метаданными, по которым клиент понимает, есть ли ещё страницы.

Поддерживаются два стиля параметров запроса:

* ``?page=2&page_size=20`` — основной, используется мобильным приложением;
* ``?limit=20&offset=40`` — устаревший, остаётся ради внешних интеграций.

Если переданы оба, приоритет у ``offset``.
"""

from django.core.paginator import Paginator

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


def _int_param(request, names, default):
    """Читает первый найденный целочисленный GET-параметр из ``names``."""
    for name in names:
        raw = request.GET.get(name)
        if raw is None or raw == "":
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default
    return default


def _resolve_page_size(request, default_page_size):
    size = _int_param(request, ("page_size", "pageSize", "limit"), default_page_size)
    if size <= 0:
        return default_page_size
    return min(size, MAX_PAGE_SIZE)


def _ensure_ordered(qs):
    """Гарантирует детерминированный порядок — иначе страницы могут пересекаться.

    У большинства моделей задан ``Meta.ordering``; страховка нужна для
    queryset'ов, у которых порядок сняли через ``order_by()`` или которые
    собраны из ``values()``.
    """
    if hasattr(qs, "ordered") and not qs.ordered:
        return qs.order_by("-pk")
    return qs


def paginate(request, qs, default_page_size=DEFAULT_PAGE_SIZE):
    """Нарезает queryset (или список) на страницу.

    Возвращает кортеж ``(items, meta)``, где ``meta`` — словарь метаданных
    для блока ``pagination`` в ответе.
    """
    page_size = _resolve_page_size(request, default_page_size)
    qs = _ensure_ordered(qs)

    offset_raw = request.GET.get("offset")
    if offset_raw not in (None, ""):
        # Устаревший limit/offset-режим: считаем номер страницы из смещения.
        offset = max(_int_param(request, ("offset",), 0), 0)
        page_number = offset // page_size + 1
    else:
        page_number = max(_int_param(request, ("page",), 1), 1)

    paginator = Paginator(qs, page_size)
    # ``get_page`` не бросает исключений: пустая страница вернёт последнюю,
    # а при полном отсутствии объектов — пустой список.
    page = paginator.get_page(page_number)

    meta = {
        "page": page.number,
        "pageSize": page_size,
        "count": paginator.count,
        "totalPages": paginator.num_pages,
        "hasNext": page.has_next(),
        "hasPrevious": page.has_previous(),
    }
    return list(page.object_list), meta


def paginated_response(request, qs, formatter, extra=None, default_page_size=DEFAULT_PAGE_SIZE):
    """Собирает тело ответа со списком и метаданными пагинации.

    ``formatter`` — функция, превращающая объект модели в словарь.
    ``extra`` — дополнительные ключи верхнего уровня (например ``stats``).
    """
    items, meta = paginate(request, qs, default_page_size=default_page_size)
    payload = {
        "results": [formatter(item) for item in items],
        "pagination": meta,
        "count": meta["count"],
    }
    if extra:
        payload.update(extra)
    return payload
