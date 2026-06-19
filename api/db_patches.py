"""Make DecimalField reads crash-proof on SQLite.

SQLite is dynamically typed, so a DecimalField column can end up holding a value that
doesn't fit the field's (max_digits, decimal_places) — e.g. Infinity/NaN or an oversized
number. Django's sqlite converter then raises ``decimal.InvalidOperation`` while *fetching*
the row, which 500s every endpoint that reads that table — and the failure spreads through
``select_related`` joins (a bad ``clientprofile.total_purchases`` took down the orders and
delivery-task lists). The data-repair migrations clean existing rows, but this guard
guarantees a single bad value can never take an endpoint down again, including values
written after a migration ran.
"""
import decimal

from django.db.backends.sqlite3.operations import DatabaseOperations


def patch_sqlite_decimal_converter():
    if getattr(DatabaseOperations, "_decimal_converter_patched", False):
        return

    original = DatabaseOperations.get_decimalfield_converter

    def get_decimalfield_converter(self, expression):
        converter = original(self, expression)
        output_field = getattr(expression, "output_field", None)
        decimal_places = getattr(output_field, "decimal_places", None)
        fallback = decimal.Decimal(0)
        if decimal_places is not None:
            fallback = fallback.quantize(decimal.Decimal(1).scaleb(-decimal_places))

        def safe_converter(value, expression, connection):
            if value is None:
                return None
            try:
                return converter(value, expression, connection)
            except (decimal.InvalidOperation, ValueError, TypeError):
                # Corrupt/out-of-range stored value: degrade to 0 instead of 500ing.
                return fallback

        return safe_converter

    DatabaseOperations.get_decimalfield_converter = get_decimalfield_converter
    DatabaseOperations._decimal_converter_patched = True
