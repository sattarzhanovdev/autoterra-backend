# Generated manually for distributor order rejection workflow.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0007_attachment"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="rejection_reason",
            field=models.TextField(blank=True, verbose_name="Причина отклонения"),
        ),
    ]
