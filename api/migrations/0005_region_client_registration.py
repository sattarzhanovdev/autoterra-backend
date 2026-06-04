# Generated manually for AutoTerra registration/routing.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0004_alter_product_sku"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Region",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(max_length=32, unique=True, verbose_name="Код")),
                ("name", models.CharField(max_length=128, unique=True, verbose_name="Название")),
                ("is_active", models.BooleanField(default=True, verbose_name="Активен")),
                (
                    "distributor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="managed_regions",
                        to="api.distributor",
                        verbose_name="Дистрибьютор",
                    ),
                ),
                (
                    "manager",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="managed_regions",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Менеджер",
                    ),
                ),
            ],
            options={
                "verbose_name": "Регион",
                "verbose_name_plural": "Регионы",
                "ordering": ("name",),
            },
        ),
        migrations.AlterField(
            model_name="clientprofile",
            name="inn",
            field=models.CharField(max_length=12, verbose_name="ИНН"),
        ),
        migrations.AddField(
            model_name="clientprofile",
            name="manager",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="managed_clients",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Менеджер",
            ),
        ),
        migrations.AddField(
            model_name="clientprofile",
            name="registration_source",
            field=models.CharField(
                choices=[
                    ("client", "Клиент"),
                    ("importer_manager", "Менеджер импортёра"),
                    ("distributor", "Дистрибьютор"),
                ],
                default="client",
                max_length=32,
                verbose_name="Источник регистрации",
            ),
        ),
        migrations.AlterField(
            model_name="clientprofile",
            name="status",
            field=models.CharField(
                choices=[
                    ("new", "Новый"),
                    ("under_review", "На проверке"),
                    ("approved", "Одобрен"),
                    ("rejected", "Отклонён"),
                    ("newClient", "Новый"),
                    ("pending", "На проверке"),
                    ("active", "Активный"),
                    ("blocked", "Заблокирован"),
                    ("archived", "Архив"),
                ],
                default="approved",
                max_length=32,
                verbose_name="Статус",
            ),
        ),
        migrations.AddConstraint(
            model_name="clientprofile",
            constraint=models.UniqueConstraint(fields=("inn", "region"), name="unique_client_inn_region"),
        ),
    ]
