# Generated manually for purchase attachments and duplicate review workflow.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0005_region_client_registration"),
    ]

    operations = [
        migrations.AlterField(
            model_name="purchase",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "На проверке"),
                    ("pending_verification", "Ожидает подтверждения"),
                    ("under_review", "Ручная проверка"),
                    ("duplicate_review", "Проверка дубля"),
                    ("verified", "Подтверждена"),
                    ("rejected", "Отклонена"),
                ],
                default="pending_verification",
                max_length=32,
                verbose_name="Статус",
            ),
        ),
        migrations.AddField(
            model_name="purchase",
            name="document_file",
            field=models.FileField(blank=True, null=True, upload_to="purchases/%Y/%m/", verbose_name="Файл документа"),
        ),
        migrations.AddField(
            model_name="purchase",
            name="document_hash",
            field=models.CharField(blank=True, db_index=True, max_length=64, verbose_name="Хэш документа"),
        ),
        migrations.AddField(
            model_name="purchase",
            name="rejection_reason",
            field=models.TextField(blank=True, verbose_name="Причина отклонения"),
        ),
    ]
