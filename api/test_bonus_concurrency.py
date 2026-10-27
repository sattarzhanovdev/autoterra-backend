from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from unittest import skipUnless

from django.db import close_old_connections, connection
from django.test import TransactionTestCase

from .test_bonus_account import _Base
from .models import BonusTransaction, ClientProfile, Order, User
from .services import bonuses


@skipUnless(connection.vendor == 'postgresql', 'Row locks require PostgreSQL')
class BonusConcurrencyTests(TransactionTestCase):
    setUp = _Base.setUp
    _client = _Base._client
    _order = _Base._order
    _credit = _Base._credit

    def test_parallel_requests_debit_order_once(self):
        self._credit(10000)
        order = self._order(50000)
        barrier = Barrier(2)
        def spend(_):
            close_old_connections()
            try:
                client = ClientProfile.objects.get(pk=self.client_profile.pk)
                current_order = Order.objects.get(pk=order.pk)
                barrier.wait(timeout=10)
                return bonuses.debit_for_order(client, current_order, Decimal(5000))
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(spend, range(2))), [Decimal(5000), Decimal(5000)])
        self.assertEqual(BonusTransaction.objects.filter(order=order, kind='order').count(), 1)
        self.assertEqual(bonuses.balance(self.client_profile), Decimal(5000))

    def test_parallel_manual_debits_cannot_overdraw(self):
        self._credit(10000)
        actor = User.objects.create_superuser('concurrent-admin', 'a@example.test', 'pw')
        barrier = Barrier(2)
        def spend(_):
            close_old_connections()
            try:
                client = ClientProfile.objects.get(pk=self.client_profile.pk)
                admin = User.objects.get(pk=actor.pk)
                barrier.wait(timeout=10)
                try:
                    bonuses.manual_debit(client, Decimal(7000), 'Выдан бонус', admin)
                    return True
                except ValueError:
                    return False
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(spend, range(2))), [False, True])
        self.assertEqual(bonuses.balance(self.client_profile), Decimal(3000))
