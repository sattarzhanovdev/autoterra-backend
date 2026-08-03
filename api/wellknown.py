"""Файлы проверки владения доменом для deep links.

Ссылка-приглашение вида https://autoterra.shop/register?ref=CODE должна
открывать приложение, а не браузер. Android разрешает это только после
проверки: при установке система скачивает /.well-known/assetlinks.json и
сверяет отпечаток подписи APK с указанным здесь. Пока файл не отдаётся или
отпечаток не совпадает, ссылка молча уходит в браузер — ошибки пользователь
не увидит.
"""

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.http import require_GET


@require_GET
def assetlinks(_request):
    """Список приложений, которым домен доверяет открывать свои ссылки."""
    fingerprints = getattr(settings, "ANDROID_APP_FINGERPRINTS", [])
    if not fingerprints:
        # Пустой список сломал бы проверку тише, чем явный отказ: Android
        # просто перестал бы открывать приложение без объяснений.
        return JsonResponse(
            {"detail": "ANDROID_APP_FINGERPRINTS не настроен"},
            status=503,
        )

    return JsonResponse(
        [
            {
                "relation": ["delegate_permission/common.handle_all_urls"],
                "target": {
                    "namespace": "android_app",
                    "package_name": settings.ANDROID_APP_PACKAGE,
                    "sha256_cert_fingerprints": fingerprints,
                },
            }
        ],
        safe=False,
    )
