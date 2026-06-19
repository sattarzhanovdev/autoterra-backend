import decimal

from django.db import connection
from django.test import TestCase

from .models import ClientProfile, Region, Distributor
from django.contrib.auth.models import User


class DecimalGuardTests(TestCase):
    """A corrupt DecimalField value stored in SQLite must not 500 reads.

    Regression for the decimal.InvalidOperation crashes on /api/distributor/orders/
    etc. caused by out-of-range / non-finite values in DecimalField columns.
    """

    def setUp(self):
        self.dist_user = User.objects.create_user(username="+79001110001", password="pw")
        self.distributor = Distributor.objects.create(
            user=self.dist_user, name="Dist", inn="1", phone="1", email="d@e.co"
        )
        self.region = Region.objects.create(code="77", name="Москва", distributor=self.distributor)
        self.profile = ClientProfile.objects.create(
            user=User.objects.create_user(username="client_user"),
            inn="1234567890", company_name="C", region=self.region,
            distributor=self.distributor, phone="1", city="Москва", contact_name="N",
        )

    def _poison(self, raw_value):
        # Write directly, bypassing the ORM, to mimic a value SQLite happily stores
        # but Django's decimal converter chokes on.
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE api_clientprofile SET total_purchases = %s WHERE id = %s",
                [raw_value, self.profile.id],
            )

    def test_oversized_value_reads_as_zero(self):
        self._poison("99999999999999")  # exceeds max_digits=12 when quantized
        obj = ClientProfile.objects.get(id=self.profile.id)
        self.assertEqual(obj.total_purchases, decimal.Decimal("0.00"))

    def test_infinity_reads_as_zero(self):
        self._poison("Infinity")
        self.assertEqual(
            ClientProfile.objects.get(id=self.profile.id).total_purchases,
            decimal.Decimal("0.00"),
        )

    def test_valid_value_still_reads_correctly(self):
        self._poison("1234.50")
        self.assertEqual(
            ClientProfile.objects.get(id=self.profile.id).total_purchases,
            decimal.Decimal("1234.50"),
        )
