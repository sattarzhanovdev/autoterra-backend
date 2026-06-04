# Generated manually for courier workflow.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0008_order_rejection_reason"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="couriertask",
            name="assigned_courier",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="courier_tasks",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Назначенный курьер",
            ),
        ),
        migrations.AddField(
            model_name="couriertask",
            name="color_request",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="courier_tasks",
                to="api.colorrequest",
                verbose_name="Заявка Color Lab",
            ),
        ),
        migrations.AddField(
            model_name="couriertask",
            name="courier_comment",
            field=models.TextField(blank=True, verbose_name="Комментарий курьера"),
        ),
        migrations.AddField(
            model_name="couriertask",
            name="order",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="courier_tasks",
                to="api.order",
                verbose_name="Связанный заказ",
            ),
        ),
        migrations.AddField(
            model_name="couriertask",
            name="status_history",
            field=models.JSONField(blank=True, default=list, verbose_name="История статусов"),
        ),
        migrations.AlterField(
            model_name="couriertask",
            name="status",
            field=models.CharField(
                choices=[
                    ("created", "Создана"),
                    ("assigned", "Назначен курьер"),
                    ("picked_up", "Забрано"),
                    ("in_progress", "В пути"),
                    ("inProgress", "В пути"),
                    ("delivered", "Доставлено"),
                    ("returned", "Возвращено"),
                    ("cancelled", "Отменено"),
                ],
                default="created",
                max_length=32,
                verbose_name="Статус",
            ),
        ),
    ]
