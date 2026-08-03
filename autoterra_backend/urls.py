from django.contrib import admin
from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path

from api.wellknown import assetlinks

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("api.urls")),
    # Android ходит именно по этому пути и только по https, без редиректов.
    path(".well-known/assetlinks.json", assetlinks),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
