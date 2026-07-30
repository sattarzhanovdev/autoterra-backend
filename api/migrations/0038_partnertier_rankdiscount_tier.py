"""Ранги клиентов становятся настраиваемыми, скидки привязываются к рангу.

Пороги переезжают из хардкода в таблицу PartnerTier, чтобы админ мог менять
их сам. Скидка теперь привязана к рангу (partner_status), а не к категории
бизнеса A/B/C — категория осталась справочным полем.
"""

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models

DEFAULT_TIERS = [
    ("Базовый", 0, "Стартовый ранг при регистрации"),
    ("Silver", 500_000, "Оборот от 500 тыс. ₽"),
    ("Gold", 2_000_000, "Оборот от 2 млн ₽"),
    ("Platinum", 5_000_000, "Оборот от 5 млн ₽"),
]


def seed_tiers(apps, schema_editor):
    PartnerTier = apps.get_model("api", "PartnerTier")
    for name, threshold, description in DEFAULT_TIERS:
        PartnerTier.objects.get_or_create(
            name=name,
            defaults={"threshold": threshold, "description": description},
        )

    # Клиенты со статусом, которого больше нет в лестнице (например
    # «Certified Partner»), сохраняют его как есть: понижать ранг молча нельзя.
    # Такой статус просто не даёт скидки, пока админ не заведёт для него ранг.


def drop_tiers(apps, schema_editor):
    PartnerTier = apps.get_model("api", "PartnerTier")
    PartnerTier.objects.filter(name__in=[name for name, _, _ in DEFAULT_TIERS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0037_alter_clientprofile_category_rankdiscount"),
    ]

    operations = [
        migrations.CreateModel(
            name="PartnerTier",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=32, unique=True, verbose_name="Название")),
                (
                    "threshold",
                    models.DecimalField(
                        decimal_places=2,
                        default=0,
                        help_text="Начиная с этой суммы клиент получает ранг. У базового ранга — 0.",
                        max_digits=14,
                        verbose_name="Порог оборота, ₽",
                    ),
                ),
                ("description", models.CharField(blank=True, max_length=255, verbose_name="Описание")),
                ("is_active", models.BooleanField(default=True, verbose_name="Активен")),
            ],
            options={
                "verbose_name": "Ранг клиента",
                "verbose_name_plural": "Ранги клиентов",
                "ordering": ("threshold",),
            },
        ),
        migrations.RunPython(seed_tiers, drop_tiers),
        # RankDiscount заведён предыдущей миграцией и данных ещё не имеет,
        # поэтому поле просто заменяем.
        migrations.AlterUniqueTogether(name="rankdiscount", unique_together=set()),
        migrations.RemoveField(model_name="rankdiscount", name="client_category"),
        migrations.AddField(
            model_name="rankdiscount",
            name="tier",
            field=models.ForeignKey(
                default=None,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="discounts",
                to="api.partnertier",
                verbose_name="Ранг клиента",
            ),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name="rankdiscount",
            name="tier",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="discounts",
                to="api.partnertier",
                verbose_name="Ранг клиента",
            ),
        ),
        migrations.AlterModelOptions(
            name="rankdiscount",
            options={
                "ordering": ("tier__threshold", "product_category"),
                "verbose_name": "Скидка по рангу",
                "verbose_name_plural": "Скидки по рангам",
            },
        ),
        migrations.AlterUniqueTogether(
            name="rankdiscount",
            unique_together={("distributor", "tier", "product_category")},
        ),
    ]
