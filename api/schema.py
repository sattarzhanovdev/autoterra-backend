from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_GET


def _ref(name):
    return {"$ref": f"#/components/schemas/{name}"}


def _array(item):
    return {"type": "array", "items": item}


def _ok(schema, description="Успешный ответ"):
    return {
        "description": description,
        "content": {"application/json": {"schema": schema}},
    }


def _created(schema, description="Объект создан"):
    return {
        "description": description,
        "content": {"application/json": {"schema": schema}},
    }


def _error(description):
    return {
        "description": description,
        "content": {"application/json": {"schema": _ref("Error")}},
    }


def _body(schema, description):
    return {
        "required": True,
        "description": description,
        "content": {"application/json": {"schema": schema}},
    }


def _secured(operation):
    operation["security"] = [{"BearerAuth": []}]
    operation.setdefault(
        "responses",
        {},
    ).update(
        {
            "401": _error("Не передан или неверен Bearer token."),
            "403": _error(
                "Пользователь найден, но для него не создан ClientProfile. "
                "Создайте профиль клиента в Django admin и привяжите к User."
            ),
        }
    )
    return operation


SCHEMAS = {
    "Error": {
        "type": "object",
        "required": ["detail"],
        "properties": {
            "detail": {
                "type": "string",
                "description": "Человекочитаемая причина ошибки.",
                "example": "Unauthorized",
            }
        },
    },
    "Health": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "example": "ok"},
            "service": {"type": "string", "example": "autoterra-api"},
        },
    },
    "LoginRequest": {
        "type": "object",
        "required": ["phone", "password"],
        "properties": {
            "phone": {
                "type": "string",
                "description": (
                    "Username пользователя. Для автосервиса обычно телефон. "
                    "Номер с 8 нормализуется в +7."
                ),
                "example": "+996222121217",
            },
            "password": {"type": "string", "format": "password", "example": "client12345"},
        },
    },
    "LoginResponse": {
        "type": "object",
        "required": ["token", "user"],
        "properties": {
            "token": {
                "type": "string",
                "description": "Токен для заголовка Authorization: Bearer <token>.",
                "example": "b7d5a2f2d1f9...",
            },
            "user": {
                "type": "object",
                "description": (
                    "Данные пользователя. Для role=autoservice доступно мобильное приложение. "
                    "Для role=distributor backend возвращает карточку distributor, но мобильные "
                    "клиентские endpoint-ы пока требуют ClientProfile."
                ),
                "properties": {
                    "id": {"type": "string", "example": "12"},
                    "phone": {"type": "string", "example": "+996222121217"},
                    "email": {"type": "string", "example": "client@example.com"},
                    "role": {"type": "string", "enum": ["autoservice", "distributor"]},
                    "status": {"type": "string", "example": "active"},
                    "distributor": _ref("Distributor"),
                },
            },
        },
    },
    "Client": {
        "type": "object",
        "description": "Профиль автосервиса. Создаётся и редактируется через Django admin.",
        "properties": {
            "id": {"type": "string", "example": "1"},
            "inn": {"type": "string", "example": "222122004503"},
            "name": {"type": "string", "example": "Кузовной сервис Бишкек"},
            "category": {"type": "string", "enum": ["a", "b", "c"], "example": "b"},
            "region": {"type": "string", "example": "Бишкек"},
            "city": {"type": "string", "example": "Бишкек"},
            "contact": {"type": "string", "example": "Даниел"},
            "phone": {"type": "string", "example": "+996222121217"},
            "distributorId": {"type": "string", "example": "2"},
            "managerId": {"type": "string", "nullable": True, "example": None},
            "status": {
                "type": "string",
                "enum": ["newClient", "pending", "active", "blocked", "archived"],
                "example": "active",
            },
            "partnerStatus": {"type": "string", "example": "Silver"},
            "totalPurchases": {"type": "number", "format": "double", "example": 128500.0},
            "createdAt": {"type": "string", "format": "date-time"},
        },
    },
    "Distributor": {
        "type": "object",
        "description": (
            "Дистрибьютор, закреплённый за регионом и клиентами. "
            "Поле userId появляется, если в admin привязан аккаунт дистрибьютора."
        ),
        "properties": {
            "id": {"type": "string", "example": "2"},
            "userId": {"type": "string", "nullable": True, "example": "7"},
            "name": {"type": "string", "example": "Daniel Sattarzhanov"},
            "inn": {"type": "string", "example": "222122004503"},
            "regions": _array({"type": "string", "example": "Бишкек"}),
            "phone": {"type": "string", "example": "+996222121217"},
            "email": {"type": "string", "format": "email", "example": "dist@example.com"},
            "isActive": {"type": "boolean", "example": True},
        },
    },
    "Store": {
        "type": "object",
        "description": "Точка самовывоза/магазин клиента у закреплённого дистрибьютора.",
        "properties": {
            "id": {"type": "string", "example": "5"},
            "name": {"type": "string", "example": "Магазин Профсоюзная"},
            "address": {"type": "string", "example": "Бишкек, ул. Киевская, 120"},
            "isActive": {"type": "boolean", "example": True},
            "createdAt": {"type": "string", "format": "date-time"},
        },
    },
    "Product": {
        "type": "object",
        "description": "Позиция ассортимента закреплённого дистрибьютора клиента. Чужие прайсы клиенту не отдаются.",
        "properties": {
            "id": {"type": "string", "example": "11"},
            "distributorId": {"type": "string", "example": "2"},
            "sku": {"type": "string", "example": "LAK-015"},
            "name": {"type": "string", "example": "Лак HS 2+1"},
            "category": {"type": "string", "example": "Лаки"},
            "brand": {"type": "string", "example": "AutoTerra"},
            "volume": {"type": "number", "format": "double", "example": 1.0},
            "price": {"type": "number", "format": "double", "example": 2800.0},
            "quantity": {"type": "integer", "example": 24},
            "status": {"type": "string", "enum": ["inStock", "low", "onOrder", "outOfStock"]},
            "updatedAt": {"type": "string", "format": "date-time"},
        },
    },
    "ReferralStats": {
        "type": "object",
        "description": "Сводка по рекомендациям клиента. Backend сверяет приглашённых по ИНН с зарегистрированными ClientProfile.",
        "properties": {
            "invitedCount": {"type": "integer", "example": 5},
            "registeredCount": {"type": "integer", "example": 3},
            "buyersCount": {"type": "integer", "example": 2},
            "giftCount": {"type": "integer", "example": 1},
            "purchaseAmount": {"type": "number", "format": "double", "example": 84500},
        },
    },
    "PurchaseItem": {
        "type": "object",
        "properties": {
            "sku": {"type": "string", "example": "LAK-015"},
            "name": {"type": "string", "example": "Лак HS 2+1"},
            "category": {"type": "string", "example": "Лаки"},
            "quantity": {"type": "integer", "example": 2},
            "volume": {"type": "number", "format": "double", "example": 1.0},
            "price": {"type": "number", "format": "double", "example": 2800.0},
            "brand": {"type": "string", "example": "AutoTerra"},
        },
    },
    "Purchase": {
        "type": "object",
        "description": "Подтверждённая или ожидающая проверки покупка/УПД.",
        "properties": {
            "id": {"type": "string", "example": "6"},
            "clientId": {"type": "string", "example": "1"},
            "distributorId": {"type": "string", "example": "2"},
            "documentNumber": {"type": "string", "example": "УПД-2026-001"},
            "date": {"type": "string", "format": "date", "example": "2026-05-20"},
            "totalAmount": {"type": "number", "format": "double", "example": 5600.0},
            "status": {"type": "string", "enum": ["pending", "verified", "rejected"]},
            "documentUrl": {"type": "string", "nullable": True, "example": None},
            "createdAt": {"type": "string", "format": "date-time"},
            "items": _array(_ref("PurchaseItem")),
        },
    },
    "OrderItem": {
        "allOf": [_ref("PurchaseItem")],
        "description": "Снимок товара на момент заказа.",
    },
    "Order": {
        "type": "object",
        "description": "Заявка клиента на заказ ассортимента у закреплённого дистрибьютора.",
        "properties": {
            "id": {"type": "string", "example": "9"},
            "clientId": {"type": "string", "example": "1"},
            "distributorId": {"type": "string", "example": "2"},
            "storeId": {"type": "string", "example": "5"},
            "storeName": {"type": "string", "example": "Магазин Профсоюзная"},
            "documentNumber": {"type": "string", "example": "ORD-00009"},
            "date": {"type": "string", "format": "date-time"},
            "totalAmount": {"type": "number", "format": "double", "example": 8400.0},
            "status": {"type": "string", "enum": ["pending", "verified"]},
            "orderStatus": {"type": "string", "enum": ["pending", "accepted", "rejected", "done"]},
            "comment": {"type": "string", "example": "Нужен счёт и аналог растворителя"},
            "documentUrl": {"type": "string", "nullable": True, "example": None},
            "createdAt": {"type": "string", "format": "date-time"},
            "items": _array(_ref("OrderItem")),
        },
    },
    "CreateOrderRequest": {
        "type": "object",
        "required": ["storeId", "items"],
        "properties": {
            "storeId": {"type": "string", "description": "ID активной точки клиента.", "example": "5"},
            "comment": {"type": "string", "example": "Подготовьте счёт"},
            "items": _array(
                {
                    "type": "object",
                    "required": ["productId", "quantity"],
                    "properties": {
                        "productId": {
                            "type": "string",
                            "description": "ID активного Product у дистрибьютора клиента.",
                            "example": "11",
                        },
                        "quantity": {"type": "integer", "minimum": 1, "example": 3},
                    },
                }
            ),
        },
    },
    "CreatePurchaseRequest": {
        "type": "object",
        "properties": {
            "documentNumber": {"type": "string", "example": "УПД-2026-001"},
            "date": {"type": "string", "format": "date", "example": "2026-05-20"},
            "totalAmount": {"type": "number", "format": "double", "example": 5600},
            "documentUrl": {"type": "string", "example": "upd_2026_001.pdf"},
            "items": _array(_ref("PurchaseItem")),
        },
    },
    "ColorRequest": {
        "type": "object",
        "description": "Заявка на подбор цвета. Забор лючка является опцией courierPickup.",
        "properties": {
            "id": {"type": "string", "example": "3"},
            "clientId": {"type": "string", "example": "1"},
            "carBrand": {"type": "string", "example": "Toyota"},
            "carModel": {"type": "string", "example": "Camry"},
            "vin": {"type": "string", "example": "JTDBT923391234567"},
            "colorCode": {"type": "string", "example": "1F7"},
            "colorName": {"type": "string", "example": "Silver Metallic"},
            "urgent": {"type": "boolean", "example": False},
            "courierPickup": {"type": "boolean", "example": True},
            "status": {"type": "string", "enum": ["created", "inProgress", "ready", "delivered"]},
            "recipe": {"type": "string", "nullable": True, "example": "Base: 65% Silver..."},
            "createdAt": {"type": "string", "format": "date-time"},
        },
    },
    "CreateColorRequestRequest": {
        "type": "object",
        "required": ["carBrand", "carModel", "vin", "colorCode"],
        "properties": {
            "carBrand": {"type": "string", "example": "Toyota"},
            "carModel": {"type": "string", "example": "Camry"},
            "vin": {"type": "string", "example": "JTDBT923391234567"},
            "colorCode": {"type": "string", "example": "1F7"},
            "colorName": {"type": "string", "example": "Silver Metallic"},
            "urgent": {"type": "boolean", "example": False},
            "courierPickup": {"type": "boolean", "example": True},
        },
    },
    "CourierTask": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "example": "8"},
            "clientId": {"type": "string", "example": "1"},
            "type": {"type": "string", "enum": ["delivery", "pickup", "return"]},
            "address": {"type": "string", "example": "Бишкек, ул. Киевская, 120"},
            "scheduledTime": {"type": "string", "format": "date-time"},
            "contactName": {"type": "string", "example": "Даниел"},
            "contactPhone": {"type": "string", "example": "+996222121217"},
            "carDescription": {"type": "string", "example": "Toyota Camry 1F7"},
            "status": {"type": "string", "enum": ["created", "assigned", "inProgress", "delivered", "returned"]},
            "courierId": {"type": "string", "nullable": True, "example": "courier-1"},
            "photoProof": {"type": "string", "nullable": True, "example": None},
            "comment": {"type": "string", "nullable": True, "example": "Позвонить за 30 минут"},
            "createdAt": {"type": "string", "format": "date-time"},
        },
    },
    "CreateCourierTaskRequest": {
        "type": "object",
        "required": ["address"],
        "properties": {
            "type": {"type": "string", "enum": ["delivery", "pickup", "return"], "example": "delivery"},
            "address": {"type": "string", "example": "Бишкек, ул. Киевская, 120"},
            "scheduledTime": {"type": "string", "format": "date-time"},
            "contactName": {"type": "string", "example": "Даниел"},
            "contactPhone": {"type": "string", "example": "+996222121217"},
            "carDescription": {"type": "string", "example": "Toyota Camry 1F7"},
            "comment": {"type": "string", "example": "Позвонить за 30 минут"},
        },
    },
    "Referral": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "example": "4"},
            "inviterId": {"type": "string", "example": "1"},
            "inviteeInn": {"type": "string", "example": "123456789012"},
            "inviteeName": {"type": "string", "example": "СТО Партнёр"},
            "region": {"type": "string", "example": "Бишкек"},
            "isRegistered": {"type": "boolean", "example": False},
            "hasPurchase": {"type": "boolean", "example": False},
            "purchaseAmount": {"type": "number", "format": "double", "example": 0},
            "conditionMet": {"type": "boolean", "example": False},
            "gift": {"type": "string", "nullable": True, "example": None},
            "createdAt": {"type": "string", "format": "date-time"},
        },
    },
    "CreateReferralRequest": {
        "type": "object",
        "required": ["inviteeInn", "inviteeName"],
        "properties": {
            "inviteeInn": {"type": "string", "example": "123456789012"},
            "inviteeName": {"type": "string", "example": "СТО Партнёр"},
            "region": {"type": "string", "example": "Бишкек"},
        },
    },
    "Ticket": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "example": "10"},
            "clientId": {"type": "string", "example": "1"},
            "question": {"type": "string", "example": "Почему подрывает лак?"},
            "category": {"type": "string", "example": "Лаки"},
            "aiAnswer": {"type": "string", "nullable": True, "example": None},
            "expertAnswer": {"type": "string", "nullable": True, "example": None},
            "status": {"type": "string", "enum": ["open", "aiAnswered", "escalated", "expertAnswered", "closed"]},
            "createdAt": {"type": "string", "format": "date-time"},
        },
    },
    "CreateTicketRequest": {
        "type": "object",
        "required": ["question"],
        "properties": {
            "question": {"type": "string", "example": "Почему подрывает лак?"},
            "category": {"type": "string", "example": "Лаки"},
        },
    },
    "Notification": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "example": "12"},
            "title": {"type": "string", "example": "Заказ принят"},
            "body": {"type": "string", "example": "Дистрибьютор принял заказ ORD-00009."},
            "type": {"type": "string", "enum": ["order", "color", "delivery", "referral", "ai", "system"]},
            "isRead": {"type": "boolean", "example": False},
            "createdAt": {"type": "string", "format": "date-time"},
        },
    },
    "KnowledgeCard": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "example": "7"},
            "problem": {"type": "string", "example": "Подрыв лака"},
            "causes": {"type": "string", "example": "Нарушена выдержка базы."},
            "solution": {"type": "string", "example": "Увеличить межслойную выдержку."},
            "skus": _array({"type": "string", "example": "LAK-015"}),
            "restrictions": {"type": "string", "nullable": True, "example": None},
            "approvingExpert": {"type": "string", "example": "Технолог AutoTerra"},
            "isApproved": {"type": "boolean", "example": True},
            "createdAt": {"type": "string", "format": "date-time"},
        },
    },
}


def _schema(request):
    server_url = request.build_absolute_uri("/api").rstrip("/")
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "AutoTerra Backend API",
            "version": "1.0.0",
            "description": (
                "API для мобильного приложения AutoTerra. Все бизнес-данные управляются "
                "через Django admin: дистрибьюторы, клиенты, магазины, ассортимент, "
                "покупки, заказы, колеровка, доставка, уведомления и рефералы.\n\n"
                "Типовой сценарий:\n"
                "1. В admin создать User.\n"
                "2. Создать Distributor и при необходимости привязать аккаунт дистрибьютора.\n"
                "3. Создать ClientProfile и привязать его к User клиента.\n"
                "4. Добавить Store и Product.\n"
                "5. Клиент логинится через /api/login/ и отправляет Bearer token в защищённые endpoint-ы."
            ),
            "contact": {"name": "AutoTerra backend"},
        },
        "servers": [
            {"url": server_url, "description": "Текущий Django server"},
            {"url": "http://192.168.51.83:8000/api", "description": "Локальная сеть для iPhone"},
            {"url": "http://127.0.0.1:8000/api", "description": "Локально на Mac"},
        ],
        "tags": [
            {"name": "System", "description": "Health check и служебные endpoint-ы."},
            {"name": "Auth", "description": "Вход и текущий пользователь."},
            {"name": "Dashboard", "description": "Сводные данные главного экрана."},
            {"name": "Order", "description": "Ассортимент, магазины и создание заказа."},
            {"name": "Purchases", "description": "История и добавление покупок/УПД."},
            {"name": "Color", "description": "Заявки на подбор цвета и опция забора лючка."},
            {"name": "Delivery", "description": "Доставка заказов и логистика лючка."},
            {"name": "Referral", "description": "Реферальная программа."},
            {"name": "Support", "description": "Вопросы эксперту и база знаний."},
            {"name": "Notifications", "description": "Уведомления клиента."},
        ],
        "paths": PATHS,
        "components": {
            "securitySchemes": {
                "BearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "token",
                    "description": "Передавайте токен из /api/login/: Authorization: Bearer <token>",
                }
            },
            "schemas": SCHEMAS,
        },
    }


PATHS = {
    "/health/": {
        "get": {
            "tags": ["System"],
            "summary": "Проверка доступности backend",
            "description": "Публичный endpoint без авторизации. Удобен для проверки, что Django server поднят.",
            "responses": {"200": _ok(_ref("Health"))},
        }
    },
    "/login/": {
        "post": {
            "tags": ["Auth"],
            "summary": "Вход пользователя",
            "description": (
                "Проверяет username/password. Для клиента требуется ClientProfile, иначе будет 403. "
                "Для дистрибьютора требуется привязка User к Distributor."
            ),
            "requestBody": _body(_ref("LoginRequest"), "Телефон/username и пароль."),
            "responses": {
                "200": _ok(_ref("LoginResponse")),
                "401": _error("Неверный телефон или пароль."),
                "403": _error("User существует, но не привязан к ClientProfile или Distributor."),
            },
        }
    },
    "/auth/me/": {
        "get": _secured(
            {
                "tags": ["Auth"],
                "summary": "Текущий клиент",
                "description": "Возвращает профиль автосервиса и закреплённого дистрибьютора.",
                "responses": {
                    "200": _ok(
                        {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "phone": {"type": "string"},
                                "email": {"type": "string"},
                                "role": {"type": "string", "example": "autoservice"},
                                "client": _ref("Client"),
                                "distributor": _ref("Distributor"),
                            },
                        }
                    )
                },
            }
        )
    },
    "/dashboard/": {
        "get": _secured(
            {
                "tags": ["Dashboard"],
                "summary": "Главный экран приложения",
                "description": "Клиент, дистрибьютор, счётчик непрочитанных уведомлений, последние покупки и активные заявки на цвет.",
                "responses": {
                    "200": _ok(
                        {
                            "type": "object",
                            "properties": {
                                "client": _ref("Client"),
                                "distributor": _ref("Distributor"),
                                "unreadCount": {"type": "integer", "example": 2},
                                "recentPurchases": _array(_ref("Purchase")),
                                "activeColorRequests": _array(_ref("ColorRequest")),
                            },
                        }
                    )
                },
            }
        )
    },
    "/stores/": {
        "get": _secured(
            {
                "tags": ["Order"],
                "summary": "Активные магазины/точки клиента",
                "description": "Используется при выборе, где клиент заберёт заказ.",
                "responses": {"200": _ok({"type": "object", "properties": {"results": _array(_ref("Store"))}})},
            }
        )
    },
    "/products/": {
        "get": _secured(
            {
                "tags": ["Order"],
                "summary": "Ассортимент закреплённого дистрибьютора",
                "description": (
                    "Возвращает только активные товары дистрибьютора клиента. "
                    "Поддерживает query-параметры category и search для фильтрации по товарной группе, "
                    "названию, артикулу или бренду."
                ),
                "parameters": [
                    {
                        "name": "category",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "string"},
                        "description": "Товарная группа, например Лаки или Грунты.",
                    },
                    {
                        "name": "search",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "string"},
                        "description": "Поиск по названию, артикулу или бренду.",
                    },
                ],
                "responses": {
                    "200": _ok(
                        {
                            "type": "object",
                            "properties": {
                                "distributor": _ref("Distributor"),
                                "categories": _array({"type": "string", "example": "Лаки"}),
                                "results": _array(_ref("Product")),
                            },
                        }
                    )
                },
            }
        )
    },
    "/order-config/": {
        "get": _secured(
            {
                "tags": ["Order"],
                "summary": "Всё для экрана заказа одним запросом",
                "description": "Клиент, дистрибьютор, магазины и ассортимент. Используется экраном 'Направить заказ'.",
                "responses": {
                    "200": _ok(
                        {
                            "type": "object",
                            "properties": {
                                "client": _ref("Client"),
                                "distributor": _ref("Distributor"),
                                "stores": _array(_ref("Store")),
                                "categories": _array({"type": "string", "example": "Грунты"}),
                                "products": _array(_ref("Product")),
                            },
                        }
                    )
                },
            }
        )
    },
    "/orders/": {
        "get": _secured(
            {
                "tags": ["Order"],
                "summary": "Список заказов клиента",
                "responses": {"200": _ok({"type": "object", "properties": {"results": _array(_ref("Order"))}})},
            }
        )
    },
    "/orders/create/": {
        "post": _secured(
            {
                "tags": ["Order"],
                "summary": "Создать заказ",
                "description": "Создаёт заказ у закреплённого дистрибьютора. Позиции заказа копируют SKU/цену/название товара на момент оформления.",
                "requestBody": _body(_ref("CreateOrderRequest"), "Магазин самовывоза, комментарий и товары."),
                "responses": {
                    "201": _created({"type": "object", "properties": {"order": _ref("Order")}}),
                    "400": _error("Не выбран магазин, товары пустые или не удалось добавить позиции."),
                    "404": _error("Магазин не найден у текущего клиента."),
                },
            }
        )
    },
    "/purchases/": {
        "get": _secured(
            {
                "tags": ["Purchases"],
                "summary": "История покупок клиента",
                "responses": {"200": _ok({"type": "object", "properties": {"results": _array(_ref("Purchase"))}})},
            }
        )
    },
    "/purchases/create/": {
        "post": _secured(
            {
                "tags": ["Purchases"],
                "summary": "Создать покупку/УПД на проверку",
                "description": "Создаёт покупку со статусом pending. Дистрибьютор и клиент берутся из токена.",
                "requestBody": _body(_ref("CreatePurchaseRequest"), "Данные документа и позиции."),
                "responses": {"201": _created({"type": "object", "properties": {"purchase": _ref("Purchase")}})},
            }
        )
    },
    "/color-requests/": {
        "get": _secured(
            {
                "tags": ["Color"],
                "summary": "Заявки на подбор цвета",
                "responses": {"200": _ok({"type": "object", "properties": {"results": _array(_ref("ColorRequest"))}})},
            }
        )
    },
    "/color-requests/create/": {
        "post": _secured(
            {
                "tags": ["Color"],
                "summary": "Создать заявку на подбор цвета",
                "description": "Если нужен забор лючка, передайте courierPickup=true. Это доп-опция колеровки, не отдельная услуга.",
                "requestBody": _body(_ref("CreateColorRequestRequest"), "Автомобиль, VIN, код цвета и опции."),
                "responses": {"201": _created({"type": "object", "properties": {"request": _ref("ColorRequest")}})},
            }
        )
    },
    "/courier-tasks/": {
        "get": _secured(
            {
                "tags": ["Delivery"],
                "summary": "Заявки доставки клиента",
                "responses": {"200": _ok({"type": "object", "properties": {"results": _array(_ref("CourierTask"))}})},
            }
        )
    },
    "/courier-tasks/create/": {
        "post": _secured(
            {
                "tags": ["Delivery"],
                "summary": "Создать доставку",
                "description": "Создаёт доставку заказа, забор или возврат лючка. Если дата не передана, backend ставит текущее время.",
                "requestBody": _body(_ref("CreateCourierTaskRequest"), "Адрес, тип доставки, контактные данные и комментарий."),
                "responses": {"201": _created({"type": "object", "properties": {"task": _ref("CourierTask")}})},
            }
        )
    },
    "/referrals/": {
        "get": _secured(
            {
                "tags": ["Referral"],
                "summary": "Приглашённые автосервисы и статистика",
                "description": (
                    "Возвращает список приглашений и сводку: сколько приглашено, сколько зарегистрировалось, "
                    "сколько сделали покупки и на какую сумму."
                ),
                "responses": {
                    "200": _ok(
                        {
                            "type": "object",
                            "properties": {
                                "stats": _ref("ReferralStats"),
                                "results": _array(_ref("Referral")),
                            },
                        }
                    )
                },
            }
        )
    },
    "/referrals/create/": {
        "post": _secured(
            {
                "tags": ["Referral"],
                "summary": "Создать приглашение",
                "requestBody": _body(_ref("CreateReferralRequest"), "ИНН, название и регион приглашённого сервиса."),
                "responses": {"201": _created({"type": "object", "properties": {"referral": _ref("Referral")}})},
            }
        )
    },
    "/tickets/": {
        "get": _secured(
            {
                "tags": ["Support"],
                "summary": "Вопросы эксперту",
                "responses": {"200": _ok({"type": "object", "properties": {"results": _array(_ref("Ticket"))}})},
            }
        )
    },
    "/tickets/create/": {
        "post": _secured(
            {
                "tags": ["Support"],
                "summary": "Задать вопрос эксперту",
                "requestBody": _body(_ref("CreateTicketRequest"), "Текст вопроса и категория."),
                "responses": {"201": _created({"type": "object", "properties": {"ticket": _ref("Ticket")}})},
            }
        )
    },
    "/notifications/": {
        "get": _secured(
            {
                "tags": ["Notifications"],
                "summary": "Уведомления клиента",
                "responses": {"200": _ok({"type": "object", "properties": {"results": _array(_ref("Notification"))}})},
            }
        )
    },
    "/notifications/read/": {
        "post": _secured(
            {
                "tags": ["Notifications"],
                "summary": "Отметить все уведомления прочитанными",
                "responses": {"200": _ok({"type": "object", "properties": {"ok": {"type": "boolean", "example": True}}})},
            }
        )
    },
    "/knowledge-cards/": {
        "get": _secured(
            {
                "tags": ["Support"],
                "summary": "Одобренные карточки базы знаний",
                "responses": {"200": _ok({"type": "object", "properties": {"results": _array(_ref("KnowledgeCard"))}})},
            }
        )
    },
}


@require_GET
def openapi_schema(request):
    return JsonResponse(_schema(request), json_dumps_params={"ensure_ascii": False, "indent": 2})


@require_GET
def swagger_ui(_request):
    return HttpResponse(
        """
<!doctype html>
<html lang="ru">
  <head>
    <meta charset="utf-8" />
    <title>AutoTerra API Docs</title>
    <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css" />
    <style>
      body { margin: 0; background: #f6f6f6; }
      .swagger-ui .topbar { display: none; }
    </style>
  </head>
  <body>
    <div id="swagger-ui"></div>
    <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
    <script>
      window.ui = SwaggerUIBundle({
        url: "/api/schema/",
        dom_id: "#swagger-ui",
        deepLinking: true,
        persistAuthorization: true,
        displayRequestDuration: true,
        defaultModelsExpandDepth: 2,
        defaultModelExpandDepth: 2,
        docExpansion: "none"
      });
    </script>
  </body>
</html>
        """,
        content_type="text/html; charset=utf-8",
    )
