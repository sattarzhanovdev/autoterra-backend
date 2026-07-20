from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db import migrations


# (table, column, max_digits, decimal_places) for the money fields reachable from
# user input. Values that don't fit these bounds (Infinity/NaN or oversized numbers)
# crash the SQLite decimal converter when the rows are read back through the ORM.
MONEY_COLUMNS = [
    ("api_purchase", "total_amount", 12, 2),
    ("api_purchaseitem", "price", 12, 2),
    ("api_purchaseitem", "volume", 8, 2),
    ("api_product", "price", 12, 2),
    ("api_product", "volume", 8, 2),
]


def _repair_value(raw, max_digits, decimal_places):
    """Return a safe replacement Decimal for a stored value, or None if it is already
    valid and needs no change. Finite-but-oversized values are clamped to the field's
    maximum (so the figure stays visible for manual review); non-finite or unparseable
    values are zeroed out."""
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
    # Use a raw cursor: the ORM's decimal converter is what crashes on these rows, so
    # reading them through it would defeat the purpose.
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        for table, column, max_digits, decimal_places in MONEY_COLUMNS:
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
        ("api", "0029_managertask_client_optional"),
    ]

    operations = [
        migrations.RunPython(repair_invalid_decimals, migrations.RunPython.noop),
    ]
