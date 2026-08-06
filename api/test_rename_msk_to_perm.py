"""Тесты переименования московских данных в пермские.

Главное, что тут проверяется, — что замена не задевает чужие слова: «мск»
лежит внутри «Томск» и «Омск», и наивный str.replace превратил бы их в
«Топерм» и «Оперм».
"""

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from api.management.commands.rename_msk_to_perm import Renamer
from api.models import Distributor, Region


class RenamerRulesTests(TestCase):
    def setUp(self):
        self.renamer = Renamer("Пермь - Урал")

    def test_russian_cases(self):
        """Падежи: у Перми они другие, простой заменой основы не обойтись."""
        self.assertEqual(self.renamer.apply("Москва"), "Пермь")
        self.assertEqual(self.renamer.apply("в Москве"), "в Перми")
        self.assertEqual(self.renamer.apply("из Москвы"), "из Перми")
        self.assertEqual(self.renamer.apply("еду в Москву"), "еду в Пермь")
        self.assertEqual(self.renamer.apply("под Москвой"), "под Пермью")

    def test_region_is_kray_not_oblast(self):
        """Пермь — центр края, а не области."""
        self.assertEqual(self.renamer.apply("Московская область"), "Пермский край")
        self.assertEqual(self.renamer.apply("Московской области"), "Пермского края")
        self.assertEqual(self.renamer.apply("Подмосковье"), "Пермский край")

    def test_short_tokens(self):
        self.assertEqual(self.renamer.apply("client1_msk"), "client1_perm")
        self.assertEqual(self.renamer.apply("msk@autoterra.ru"), "perm@autoterra.ru")
        self.assertEqual(self.renamer.apply("AutoTerra МСК"), "AutoTerra ПРМ")
        self.assertEqual(self.renamer.apply("VIN123MSK"), "VIN123PERM")

    def test_other_cities_untouched(self):
        """Ради этого теста и написаны регулярки с проверкой границ."""
        for word in ("Томск", "Омск", "Смоленск", "Мурманск", "г. Омск, ул. Мира"):
            self.assertEqual(self.renamer.apply(word), word)

    def test_json_walk(self):
        value = {"regions": ["Москва - Центральный", "Казань - Поволжье"], "count": 2}
        self.assertEqual(
            self.renamer.walk_json(value),
            {"regions": ["Пермь - Урал", "Казань - Поволжье"], "count": 2},
        )


class RenameCommandTests(TestCase):
    def setUp(self):
        self.region = Region.objects.create(code="77", name="Москва - Центральный")
        self.distributor = Distributor.objects.create(
            name="AutoTerra МСК",
            inn="7700000000",
            phone="+70000000000",
            email="msk@autoterra.ru",
            regions=["Москва - Центральный"],
        )
        self.user = User.objects.create_user("distributor_msk", password="x")

    def test_dry_run_changes_nothing(self):
        call_command("rename_msk_to_perm")
        self.region.refresh_from_db()
        self.user.refresh_from_db()
        self.assertEqual(self.region.name, "Москва - Центральный")
        self.assertEqual(self.region.code, "77")
        self.assertEqual(self.user.username, "distributor_msk")

    def test_apply_renames_everything(self):
        call_command("rename_msk_to_perm", "--apply")
        self.region.refresh_from_db()
        self.distributor.refresh_from_db()
        self.user.refresh_from_db()

        self.assertEqual(self.region.name, "Пермь - Урал")
        self.assertEqual(self.region.code, "59")
        self.assertEqual(self.distributor.name, "AutoTerra ПРМ")
        self.assertEqual(self.distributor.email, "perm@autoterra.ru")
        self.assertEqual(self.distributor.regions, ["Пермь - Урал"])
        self.assertEqual(self.user.username, "distributor_perm")

    def test_keep_logins(self):
        call_command("rename_msk_to_perm", "--apply", "--keep-logins")
        self.region.refresh_from_db()
        self.user.refresh_from_db()
        self.assertEqual(self.region.name, "Пермь - Урал")
        self.assertEqual(self.user.username, "distributor_msk")

    def test_custom_region_name_and_code(self):
        call_command(
            "rename_msk_to_perm",
            "--apply",
            "--region-name=Пермь - Прикамье",
            "--region-code=159",
        )
        self.region.refresh_from_db()
        self.assertEqual(self.region.name, "Пермь - Прикамье")
        self.assertEqual(self.region.code, "159")

    def test_password_hash_never_touched(self):
        """Хеш пароля — тоже CharField, но трогать его нельзя."""
        before = User.objects.get(pk=self.user.pk).password
        call_command("rename_msk_to_perm", "--apply")
        self.assertEqual(User.objects.get(pk=self.user.pk).password, before)
        self.assertTrue(User.objects.get(pk=self.user.pk).check_password("x"))


class RenameConflictTests(TestCase):
    def test_aborts_when_two_regions_collide(self):
        """Два московских региона метят в один код — записывать нельзя."""
        Region.objects.create(code="77", name="Москва - Центральный")
        Region.objects.create(code="50", name="Москва и МО")

        with self.assertRaises(CommandError):
            call_command("rename_msk_to_perm", "--apply")

        # Транзакция не начиналась: обе записи остались нетронутыми.
        self.assertTrue(Region.objects.filter(code="77").exists())
        self.assertTrue(Region.objects.filter(code="50").exists())

    def test_aborts_when_new_login_taken(self):
        User.objects.create_user("client1_msk", password="x")
        User.objects.create_user("client1_perm", password="x")

        with self.assertRaises(CommandError):
            call_command("rename_msk_to_perm", "--apply")

        self.assertTrue(User.objects.filter(username="client1_msk").exists())
