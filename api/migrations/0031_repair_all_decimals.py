from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db import migrations, models


def _repair_value(raw, max_digits, decimal_places):
    """Return a safe replacement Decimal for a stored value, or None if it is already
    valid and needs no change. Finite-but-oversized values are clamped to the field's
    maximum; non-finite or unparseable values are zeroed out.

    See migrations 0030: the SQLite backend crashes with decimal.InvalidOperation when
    it reads a value that doesn't fit a DecimalField's (max_digits, decimal_places) and
    quantizes it. 0030 only covered a few tables; this repairs every DecimalField so a
    poisoned row can't take down any endpoint (e.g. a bad client.total_purchases pulled
    in via select_related crashed the orders / delivery-tasks lists)."""
    quant = Decimal(1).scaleb(-decimal_places)
    limit = Decimal(10) ** (max_digits - decimal_places)
    max_value = limit - quant
    try:
        amount = Decimal(str(raw))
        if amount.is_finite():
            quantized = amount.quantize(quant, rounding=ROUND_HALF_UP)
            if abs(quantized) < limit:
                return None  # already valid, leave it alone
            return max_value if quantized > 0 else -max_value
    except (InvalidOperation, ValueError, TypeError):
        pass
    return Decimal("0")


def repair_invalid_decimals(apps, schema_editor):
    # Discover every DecimalField column on every model in the app, so this stays
    # correct as the schema grows. Use a raw cursor: the ORM's decimal converter is
    # exactly what crashes on these rows.
    columns = []
    for model in apps.get_app_config("api").get_models():
        table = model._meta.db_table
        for field in model._meta.get_fields():
            if isinstance(field, models.DecimalField):
                columns.append((table, field.column, field.max_digits, field.decimal_places))

    connection = schema_editor.connection
    with connection.cursor() as cursor:
        for table, column, max_digits, decimal_places in columns:
            try:
                cursor.execute("SELECT id, {} FROM {}".format(column, table))
                rows = cursor.fetchall()
            except Exception:
                continue  # table/column not present on this database
            for pk, raw in rows:
                if raw is None:
                    continue
                replacement = _repair_value(raw, max_digits, decimal_places)
                if replacement is None:
                    continue
                cursor.execute(
                    "UPDATE {} SET {} = %s WHERE id = %s".format(table, column),
                    [str(replacement), pk],
                )


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0030_repair_invalid_decimals"),
    ]

    operations = [
        migrations.RunPython(repair_invalid_decimals, migrations.RunPython.noop),
    ]
