from django.contrib import admin
from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path

from api.wellknown import assetlinks
from api.order_email_views import confirm_order_from_email

urlpatterns = [
    path("order-action/confirm/", confirm_order_from_email, name="order-email-confirm"),
    path("admin/", admin.site.urls),
    path("api/", include("api.urls")),
    # Android ходит именно по этому пути и только по https, без редиректов.
    path(".well-known/assetlinks.json", assetlinks),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
