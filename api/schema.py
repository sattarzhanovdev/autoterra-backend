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
                    "Username пользователя. Номер с 8 нормализуется в +7."
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
                "properties": {
                    "id": {"type": "string", "example": "12"},
                    "phone": {"type": "string", "example": "+996222121217"},
                    "email": {"type": "string", "example": "client@example.com"},
                    "role": {"type": "string", "enum": ["autoservice", "distributor", "courier", "expert"]},
                    "status": {"type": "string", "example": "active"},
                },
            },
        },
    },
    "Client": {
        "type": "object",
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
            "managerId": {"type": "string", "nullable": True},
            "status": {"type": "string", "example": "active"},
            "partnerStatus": {"type": "string", "example": "Silver"},
            "totalPurchases": {"type": "number", "example": 128500.0},
            "createdAt": {"type": "string", "format": "date-time"},
        },
    },
    "Distributor": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "example": "2"},
            "userId": {"type": "string", "nullable": True},
            "name": {"type": "string", "example": "Daniel Sattarzhanov"},
            "inn": {"type": "string", "example": "222122004503"},
            "regions": _array({"type": "string"}),
            "phone": {"type": "string", "example": "+996222121217"},
            "email": {"type": "string", "format": "email", "example": "dist@example.com"},
            "isActive": {"type": "boolean", "example": True},
        },
    },
    "Attachment": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "url": {"type": "string"},
            "name": {"type": "string"},
            "fileType": {"type": "string"},
            "uploadedAt": {"type": "string", "format": "date-time"},
            "description": {"type": "string", "nullable": True},
        }
    },
    "ExpertTicket": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "clientId": {"type": "string"},
            "clientName": {"type": "string"},
            "question": {"type": "string"},
            "category": {"type": "string"},
            "risk": {"type": "string", "enum": ["low", "medium", "high"]},
            "status": {"type": "string", "enum": ["open", "aiAnswered", "escalated", "expertAnswered", "closed"]},
            "aiDraftAnswer": {"type": "string", "nullable": True},
            "aiAnswer": {"type": "string", "nullable": True},
            "expertAnswer": {"type": "string", "nullable": True},
            "linkedKnowledgeCardId": {"type": "string", "nullable": True},
            "similarCases": _array({"type": "string"}),
            "createdAt": {"type": "string", "format": "date-time"},
            "updatedAt": {"type": "string", "format": "date-time"},
            "attachments": _array(_ref("Attachment")),
        },
    },
    "CreateExpertTicketRequest": {
        "type": "object",
        "required": ["question", "category"],
        "properties": {
            "question": {"type": "string", "example": "Почему подрывает лак?"},
            "category": {"type": "string", "example": "Дефекты"},
            "risk": {"type": "string", "enum": ["low", "medium", "high"], "default": "low"},
        },
    },
    "KnowledgeCard": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "title": {"type": "string"},
            "category": {"type": "string"},
            "problem": {"type": "string"},
            "causes": {"type": "string", "nullable": True},
            "solution": {"type": "string"},
            "skus": _array({"type": "string"}),
            "restrictions": {"type": "string", "nullable": True},
            "status": {"type": "string", "enum": ["draft", "approved", "rejected", "archived"]},
            "isApproved": {"type": "boolean"},
            "createdBy": {"type": "string", "nullable": True},
            "approvedBy": {"type": "string", "nullable": True},
            "revisionHistory": _array({"type": "object"}),
            "createdAt": {"type": "string", "format": "date-time"},
            "updatedAt": {"type": "string", "format": "date-time"},
        },
    },
    "AiChatResponse": {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "sourceId": {"type": "string", "nullable": True},
            "suggestEscalation": {"type": "boolean"},
        }
    },
}

PATHS = {
    "/health/": {
        "get": {
            "tags": ["System"],
            "responses": {"200": _ok(_ref("Health"))},
        }
    },
    "/login/": {
        "post": {
            "tags": ["Auth"],
            "requestBody": _body(_ref("LoginRequest"), "Логин"),
            "responses": {"200": _ok(_ref("LoginResponse"))},
        }
    },
    "/tickets/": {
        "get": _secured({
            "tags": ["Support"],
            "responses": {"200": _ok({"type": "object", "properties": {"results": _array(_ref("ExpertTicket"))}})},
        })
    },
    "/tickets/create/": {
        "post": _secured({
            "tags": ["Support"],
            "requestBody": _body(_ref("CreateExpertTicketRequest"), "Новое обращение"),
            "responses": {"201": _created({"type": "object", "properties": {"ticket": _ref("ExpertTicket")}})},
        })
    },
    "/tickets/{ticket_id}/expert-answer/": {
        "post": _secured({
            "tags": ["Expert"],
            "parameters": [{"name": "ticket_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "requestBody": _body({"type": "object", "properties": {"answer": {"type": "string"}, "status": {"type": "string"}, "createKnowledgeCard": {"type": "boolean"}}}, "Ответ эксперта"),
            "responses": {"200": _ok({"type": "object", "properties": {"ticket": _ref("ExpertTicket")}})},
        })
    },
    "/knowledge-cards/": {
        "get": _secured({
            "tags": ["Support"],
            "responses": {"200": _ok({"type": "object", "properties": {"results": _array(_ref("KnowledgeCard"))}})},
        })
    },
    "/ai/chat/": {
        "post": _secured({
            "tags": ["Support"],
            "requestBody": _body({"type": "object", "properties": {"message": {"type": "string"}}}, "Вопрос к AI"),
            "responses": {"200": _ok(_ref("AiChatResponse"))},
        })
    },
}

def _schema(request):
    server_url = request.build_absolute_uri("/api").rstrip("/")
    return {
        "openapi": "303",
        "info": {"title": "AutoTerra Q&A Refined API", "version": "1.1.0"},
        "servers": [{"url": server_url}],
        "paths": PATHS,
        "components": {
            "securitySchemes": {"BearerAuth": {"type": "http", "scheme": "bearer"}},
            "schemas": SCHEMAS,
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
  </head>
  <body>
    <div id="swagger-ui"></div>
    <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
    <script>
      window.ui = SwaggerUIBundle({
        url: "/api/schema/",
        dom_id: "#swagger-ui",
        persistAuthorization: true,
      });
    </script>
  </body>
</html>
        """,
        content_type="text/html; charset=utf-8",
    )
