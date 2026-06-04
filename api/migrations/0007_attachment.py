# Generated manually for universal file attachments.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0006_purchase_attachment_duplicates"),
        ("contenttypes", "0002_remove_content_type_name"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Attachment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("file", models.FileField(upload_to="attachments/%Y/%m/", verbose_name="Файл")),
                (
                    "file_type",
                    models.CharField(
                        choices=[
                            ("image", "Изображение"),
                            ("pdf", "PDF"),
                            ("video", "Видео"),
                            ("document", "Документ"),
                        ],
                        max_length=32,
                        verbose_name="Тип файла",
                    ),
                ),
                ("uploaded_at", models.DateTimeField(auto_now_add=True, verbose_name="Загружен")),
                ("object_id", models.PositiveIntegerField()),
                ("description", models.CharField(blank=True, max_length=255, verbose_name="Описание")),
                (
                    "content_type",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="contenttypes.contenttype"),
                ),
                (
                    "uploaded_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="uploaded_attachments",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Загрузил",
                    ),
                ),
            ],
            options={
                "verbose_name": "Вложение",
                "verbose_name_plural": "Вложения",
                "ordering": ("-uploaded_at",),
            },
        ),
        migrations.AddIndex(
            model_name="attachment",
            index=models.Index(fields=["content_type", "object_id"], name="api_attachm_content_21f603_idx"),
        ),
    ]
