from django.apps import AppConfig


class ApiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "api"
    verbose_name = "Данные платформы"

    def ready(self) -> None:
        import api.signals  # noqa: F401 — registers all signal handlers
        from api.db_patches import patch_sqlite_decimal_converter

        patch_sqlite_decimal_converter()
