"""Аналитика закупок: срезы, фильтры и выгрузка.

Отчёт складывает деньги, поэтому проверяем не только «что-то посчиталось», но
и границы: непроверенные документы в оборот не идут, фильтры не протекают друг
через друга, а менеджер не видит чужие регионы.
"""

import csv
import io
from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, TestCase

from .models import (
    AuthToken,
    ClientProfile,
    Distributor,
    Profile,
    Purchase,
    PurchaseItem,
    Region,
)
from .services import purchase_analytics as analytics


class _Base(TestCase):
    def setUp(self):
        self.http = Client()
        self.dist_msk = Distributor.objects.create(
            name="Дист МСК", inn="7700000010", phone="1", email="msk@e.co"
        )
        self.dist_kzn = Distributor.objects.create(
            name="Дист КЗН", inn="7700000011", phone="2", email="kzn@e.co"
        )
        self.msk = Region.objects.create(code="77", name="Москва", distributor=self.dist_msk)
        self.kzn = Region.objects.create(code="16", name="Казань", distributor=self.dist_kzn)

        self.client_msk = self._client("+79001110001", "1000000001", self.msk, self.dist_msk, "СТО Москва")
        self.client_kzn = self._client("+79001110002", "1000000002", self.kzn, self.dist_kzn, "СТО Казань")

    def _client(self, username, inn, region, distributor, name):
        user = User.objects.create_user(username=username, password="pw")
        return ClientProfile.objects.create(
            user=user, inn=inn, company_name=name, contact_name="И",
            phone=username, region=region, city="Город",
            distributor=distributor, status="active",
        )

    def _purchase(self, client, distributor, when, amount, status="verified", items=()):
        purchase = Purchase.objects.create(
            client=client, distributor=distributor,
            document_number=f"D{Purchase.objects.count() + 1}",
            date=when, total_amount=Decimal(amount), status=status,
        )
        for sku, name, qty, price in items:
            PurchaseItem.objects.create(
                purchase=purchase, sku=sku, name=name,
                quantity=qty, price=Decimal(price),
            )
        return purchase

    def _manager(self, username="manager", regions=(), is_global=True):
        """Глобальный видит всю Россию — это роль admin, как у manager_main.
        Роль manager региональная: ей видны только закреплённые регионы.
        """
        user = User.objects.create_user(username=username, password="pw")
        Profile.objects.update_or_create(
            user=user, defaults={"role": "admin" if is_global else "manager"}
        )
        for region in regions:
            region.manager = user
            region.save(update_fields=["manager"])
        return AuthToken.objects.create(key=f"tok-{username}", user=user).key


class TotalsTests(_Base):
    def setUp(self):
        super().setUp()
        self._purchase(self.client_msk, self.dist_msk, date(2026, 5, 10), 10_000,
                       items=[("A-1", "Краска", 2, 3000), ("B-1", "Грунт", 1, 4000)])
        self._purchase(self.client_kzn, self.dist_kzn, date(2026, 6, 20), 30_000,
                       items=[("A-1", "Краска", 5, 6000)])

    def test_totals_sum_only_verified(self):
        """Непроверенный документ — ещё не продажа, в оборот не идёт."""
        self._purchase(self.client_msk, self.dist_msk, date(2026, 6, 25), 99_000,
                       status="pending_verification", items=[("A-1", "Краска", 1, 99000)])

        totals = analytics.totals(analytics.Filters())

        self.assertEqual(totals["count"], 2)
        self.assertEqual(totals["amount"], Decimal("40000.00"))

    def test_average_check(self):
        totals = analytics.totals(analytics.Filters())

        self.assertEqual(totals["average"], Decimal("20000.00"))

    def test_distinct_clients_are_counted(self):
        self._purchase(self.client_msk, self.dist_msk, date(2026, 6, 1), 5_000)

        self.assertEqual(analytics.totals(analytics.Filters())["clients"], 2)

    def test_quantity_comes_from_items(self):
        self.assertEqual(analytics.totals(analytics.Filters())["quantity"], 8)


class FilterTests(_Base):
    def setUp(self):
        super().setUp()
        self._purchase(self.client_msk, self.dist_msk, date(2026, 5, 10), 10_000,
                       items=[("A-1", "Краска", 2, 3000)])
        self._purchase(self.client_kzn, self.dist_kzn, date(2026, 6, 20), 30_000,
                       items=[("B-1", "Грунт", 5, 6000)])

    def test_filter_by_region(self):
        totals = analytics.totals(analytics.Filters(region_id=self.msk.id))

        self.assertEqual(totals["count"], 1)
        self.assertEqual(totals["amount"], Decimal("10000.00"))

    def test_filter_by_distributor(self):
        totals = analytics.totals(analytics.Filters(distributor_id=self.dist_kzn.id))

        self.assertEqual(totals["amount"], Decimal("30000.00"))

    def test_filter_by_dates(self):
        totals = analytics.totals(
            analytics.Filters(date_from=date(2026, 6, 1), date_to=date(2026, 6, 30))
        )

        self.assertEqual(totals["count"], 1)
        self.assertEqual(totals["amount"], Decimal("30000.00"))

    def test_filter_by_sku_keeps_only_that_sku(self):
        """По артикулу нужны позиции именно этого SKU, а не весь документ."""
        self._purchase(self.client_msk, self.dist_msk, date(2026, 6, 5), 9_000,
                       items=[("A-1", "Краска", 1, 3000), ("C-9", "Лак", 1, 6000)])

        rows = analytics.by_sku(analytics.Filters(sku="A-1"))

        self.assertEqual([r["sku"] for r in rows], ["A-1"])
        self.assertEqual(rows[0]["quantity"], 3)

    def test_sku_filter_is_case_insensitive(self):
        self.assertEqual(len(analytics.by_sku(analytics.Filters(sku="a-1"))), 1)

    def test_filters_combine(self):
        # Регион Москвы и период июня — под оба условия ничего не подходит.
        totals = analytics.totals(
            analytics.Filters(region_id=self.msk.id, date_from=date(2026, 6, 1))
        )

        self.assertEqual(totals["count"], 0)
        self.assertEqual(totals["amount"], Decimal("0.00"))

    def test_status_filter_can_show_unverified(self):
        self._purchase(self.client_msk, self.dist_msk, date(2026, 6, 25), 7_000,
                       status="pending_verification")

        totals = analytics.totals(analytics.Filters(status="pending_verification"))

        self.assertEqual(totals["count"], 1)
        self.assertEqual(totals["amount"], Decimal("7000.00"))


class BreakdownTests(_Base):
    def setUp(self):
        super().setUp()
        self._purchase(self.client_msk, self.dist_msk, date(2026, 5, 10), 10_000,
                       items=[("A-1", "Краска", 2, 3000)])
        self._purchase(self.client_msk, self.dist_msk, date(2026, 6, 11), 25_000,
                       items=[("A-1", "Краска", 3, 3000)])
        self._purchase(self.client_kzn, self.dist_kzn, date(2026, 6, 20), 30_000,
                       items=[("B-1", "Грунт", 1, 30000)])

    def test_by_sku_sums_quantity_across_documents(self):
        rows = {r["sku"]: r for r in analytics.by_sku(analytics.Filters())}

        self.assertEqual(rows["A-1"]["quantity"], 5)
        self.assertEqual(rows["A-1"]["documents"], 2)
        self.assertEqual(rows["A-1"]["amount"], Decimal("15000.00"))

    def test_by_client_is_sorted_by_amount(self):
        rows = analytics.by_client(analytics.Filters())

        self.assertEqual(rows[0]["client__company_name"], "СТО Москва")
        self.assertEqual(rows[0]["amount"], Decimal("35000.00"))

    def test_by_region_counts_distinct_clients(self):
        rows = {r["client__region__name"]: r for r in analytics.by_region(analytics.Filters())}

        self.assertEqual(rows["Москва"]["clients"], 1)
        self.assertEqual(rows["Москва"]["documents"], 2)

    def test_by_month_groups_by_purchase_date(self):
        rows = analytics.by_month(analytics.Filters())

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["month"].month, 5)
        self.assertEqual(rows[1]["amount"], Decimal("55000.00"))


class ExportTests(_Base):
    def setUp(self):
        super().setUp()
        self._purchase(self.client_msk, self.dist_msk, date(2026, 5, 10), 10_000,
                       items=[("A-1", "Краска", 2, 3000), ("B-1", "Грунт", 1, 4000)])

    def test_rows_are_per_item_not_per_document(self):
        """Ради артикулов выгрузка и делается — иначе SKU в файле не видно."""
        rows = list(analytics.export_rows(analytics.Filters()))

        self.assertEqual(len(rows), 2)

    def test_row_carries_region_distributor_and_sku(self):
        row = dict(zip([title for title, _ in analytics.COLUMNS],
                       list(analytics.export_rows(analytics.Filters()))[0]))

        self.assertEqual(row["Регион"], "Москва")
        self.assertEqual(row["Дистрибьютор"], "Дист МСК")
        self.assertIn(row["Артикул"], {"A-1", "B-1"})
        self.assertEqual(row["Сумма документа"], 10000.0)

    def test_line_total_is_price_times_quantity(self):
        rows = {r[7]: r for r in analytics.export_rows(analytics.Filters())}

        self.assertEqual(rows["A-1"][13], 6000.0)

    def test_csv_has_header_and_bom(self):
        response = analytics.to_csv(analytics.Filters())
        body = response.content.decode("utf-8")

        self.assertTrue(body.startswith("﻿"), "Excel без BOM ломает кириллицу")
        reader = csv.reader(io.StringIO(body.lstrip("﻿")), delimiter=";")
        self.assertEqual(next(reader)[0], "Дата")

    def test_xlsx_is_returned_as_attachment(self):
        response = analytics.to_xlsx(analytics.Filters())

        self.assertIn("spreadsheetml", response["Content-Type"])
        self.assertIn("attachment", response["Content-Disposition"])

    def test_export_respects_filters(self):
        rows = list(analytics.export_rows(analytics.Filters(sku="A-1")))

        self.assertEqual(len(rows), 1)


class ManagerEndpointTests(_Base):
    def setUp(self):
        super().setUp()
        self._purchase(self.client_msk, self.dist_msk, date(2026, 5, 10), 10_000,
                       items=[("A-1", "Краска", 2, 3000)])
        self._purchase(self.client_kzn, self.dist_kzn, date(2026, 6, 20), 30_000,
                       items=[("B-1", "Грунт", 1, 30000)])

    def _get(self, token, query=""):
        return self.http.get(
            f"/api/analytics/purchases/{query}", HTTP_AUTHORIZATION=f"Bearer {token}"
        )

    def test_global_manager_sees_everything(self):
        data = self._get(self._manager()).json()

        self.assertEqual(data["totals"]["count"], 2)
        self.assertEqual(data["totals"]["amount"], 40000.0)

    def test_regional_manager_sees_only_own_region(self):
        """Права важнее фильтра: чужой регион не должен протечь в отчёт."""
        token = self._manager("mgr_msk", regions=[self.msk], is_global=False)

        data = self._get(token).json()

        self.assertEqual(data["totals"]["count"], 1)
        self.assertEqual([r["region"] for r in data["byRegion"]], ["Москва"])

    def test_regional_manager_cannot_request_foreign_region(self):
        token = self._manager("mgr_msk2", regions=[self.msk], is_global=False)

        data = self._get(token, f"?region={self.kzn.id}").json()

        self.assertEqual(data["totals"]["count"], 0)

    def test_client_has_no_access(self):
        token = AuthToken.objects.create(key="client-tok", user=self.client_msk.user).key

        self.assertEqual(self._get(token).status_code, 403)

    def test_anonymous_has_no_access(self):
        self.assertEqual(self.http.get("/api/analytics/purchases/").status_code, 401)

    def test_export_returns_a_file(self):
        response = self._get(self._manager(), "?export=csv")

        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])

    def test_filters_are_echoed_back(self):
        data = self._get(self._manager(), "?sku=A-1&date_from=2026-05-01").json()

        self.assertEqual(data["filters"]["sku"], "A-1")
        self.assertEqual(data["filters"]["date_from"], "2026-05-01")

    def test_dates_accept_russian_format(self):
        # В формах менеджеры набирают 20.06.2026 — это должно работать.
        data = self._get(self._manager(), "?date_from=20.06.2026").json()

        self.assertEqual(data["totals"]["count"], 1)


class AdminPageTests(_Base):
    def setUp(self):
        super().setUp()
        self._purchase(self.client_msk, self.dist_msk, date(2026, 5, 10), 10_000,
                       items=[("A-1", "Краска", 2, 3000)])
        self.admin = User.objects.create_superuser("root", "root@e.co", "pw")
        self.http.force_login(self.admin)

    def test_page_opens(self):
        response = self.http.get("/admin/api/authtoken/purchases-analytics/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Аналитика закупок")

    def test_page_shows_sku_row(self):
        response = self.http.get("/admin/api/authtoken/purchases-analytics/")

        self.assertContains(response, "A-1")

    def test_export_from_page(self):
        response = self.http.get(
            "/admin/api/authtoken/purchases-analytics/?export=csv"
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])

    def test_region_filter_actually_applies(self):
        """Раньше фильтр по региону молча не работал: падал на ValueError."""
        response = self.http.get(
            f"/admin/api/authtoken/purchases-analytics/?region={self.kzn.id}"
        )

        self.assertEqual(response.context["totals"]["count"], 0)
