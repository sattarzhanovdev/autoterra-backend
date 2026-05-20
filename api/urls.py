from django.urls import path

from . import schema, views

urlpatterns = [
    path("docs/", schema.swagger_ui),
    path("schema/", schema.openapi_schema),
    path("health/", views.health),
    path("login/", views.login),
    path("auth/me/", views.me),
    path("dashboard/", views.dashboard),
    path("stores/", views.stores),
    path("products/", views.products),
    path("order-config/", views.order_config),
    path("orders/", views.orders),
    path("orders/create/", views.create_order),
    path("purchases/", views.purchases),
    path("purchases/create/", views.create_purchase),
    path("color-requests/", views.color_requests),
    path("color-requests/create/", views.create_color_request),
    path("courier-tasks/", views.courier_tasks),
    path("courier-tasks/create/", views.create_courier_task),
    path("referrals/", views.referrals),
    path("referrals/create/", views.create_referral),
    path("tickets/", views.tickets),
    path("tickets/create/", views.create_ticket),
    path("notifications/", views.notifications),
    path("notifications/read/", views.mark_notifications_read),
    path("knowledge-cards/", views.knowledge_cards),
]
