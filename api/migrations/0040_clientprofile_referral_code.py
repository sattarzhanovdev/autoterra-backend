"""Личный реферальный код у каждого клиента.

До этого код в приложении был захардкожен — один и тот же у всех, и по нему
нельзя было связать пришедшего с пригласившим.
"""

import secrets

from django.db import migrations, models

ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def backfill_codes(apps, schema_editor):
    """Выдаёт коды уже существующим клиентам — иначе поле останется пустым."""
    ClientProfile = apps.get_model("api", "ClientProfile")

    taken = set(
        ClientProfile.objects.exclude(referral_code=None).values_list("referral_code", flat=True)
    )
    for client in ClientProfile.objects.filter(referral_code=None):
        for _ in range(50):
            code = "AT-" + "".join(secrets.choice(ALPHABET) for _ in range(6))
            if code not in taken:
                taken.add(code)
                client.referral_code = code
                client.save(update_fields=["referral_code"])
                break


def clear_codes(apps, schema_editor):
    apps.get_model("api", "ClientProfile").objects.update(referral_code=None)


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0039_alter_clientprofile_partner_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="clientprofile",
            name="referral_code",
            field=models.CharField(
                blank=True,
                db_index=True,
                max_length=16,
                null=True,
                unique=True,
                verbose_name="Реферальный код",
            ),
        ),
        migrations.RunPython(backfill_codes, clear_codes),
    ]
