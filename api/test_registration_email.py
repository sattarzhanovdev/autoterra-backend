"""Письмо о новой регистрации клиента.

Проверяем, что письмо действительно уходит, содержит данные клиента и — что
важнее — что его отправка не может сломать саму регистрацию.
"""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, TestCase, TransactionTestCase, override_settings

from .models import ClientProfile, Distributor, Region, Store

LOCMEM = "django.core.mail.backends.locmem.EmailBackend"


@override_settings(EMAIL_BACKEND=LOCMEM, DEFAULT_FROM_EMAIL="no-reply@autoterra.ru")
class RegistrationEmailTests(TestCase):
    def setUp(self):
        self.http = Client()
        self.distributor = Distributor.objects.create(
            name="Дист МСК", inn="1112223334", phone="1", email="d@e.co"
        )
        self.manager = User.objects.create_user(username="manager", password="pw")
        self.region = Region.objects.create(
            code="77", name="Москва — Центральный",
            distributor=self.distributor, manager=self.manager,
        )
        mail.outbox = []

    def _register(self, **overrides):
        payload = {
            "username": "+79001112233",
            "password": "verysecret123",
            "inn": "5556667778",
            "region_id": str(self.region.id),
            "company_name": "ООО «Ромашка»",
            "contact_name": "Иванов Иван Иванович",
            "store_address": "Москва, пр. Мира 22",
        }
        payload.update(overrides)
        return self.http.post(
            "/api/register/", data=payload, content_type="application/json"
        )

    # ── Письмо уходит ────────────────────────────────────────────────────────

    def test_registration_sends_one_email(self):
        response = self._register()
        self.assertEqual(response.status_code, 201, response.content)

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["zavtoterra@yandex.ru"])
        self.assertEqual(message.from_email, "no-reply@autoterra.ru")

    def test_subject_names_company_and_inn(self):
        self._register()
        subject = mail.outbox[0].subject
        self.assertIn("ООО «Ромашка»", subject)
        self.assertIn("5556667778", subject)

    def test_plain_text_part_has_all_fields(self):
        self._register()
        body = mail.outbox[0].body

        for expected in [
            "ООО «Ромашка»",
            "5556667778",
            "Москва — Центральный",
            "+79001112233",
            "Иванов Иван Иванович",
        ]:
            self.assertIn(expected, body, f"в тексте письма нет «{expected}»")

    def test_html_part_attached(self):
        self._register()
        alternatives = mail.outbox[0].alternatives
        self.assertEqual(len(alternatives), 1)

        html, mimetype = alternatives[0]
        self.assertEqual(mimetype, "text/html")
        self.assertIn("Новая регистрация", html)
        self.assertIn("ООО «Ромашка»", html)
        self.assertIn("Иванов Иван Иванович", html)

    def test_status_is_human_readable(self):
        self._register()
        # Первая регистрация ИНН — статус «Новый», а не сырое "new".
        self.assertIn("Новый", mail.outbox[0].body)

    def test_branch_registration_reports_review_status(self):
        other_region = Region.objects.create(
            code="16", name="Казань", distributor=self.distributor
        )
        other_user = User.objects.create_user(username="+79009998877", password="pw")
        ClientProfile.objects.create(
            user=other_user, inn="5556667778", company_name="Ромашка Казань",
            contact_name="Пётр", phone="+79009998877",
            region=other_region, city="Казань", distributor=self.distributor,
        )
        mail.outbox = []

        self._register()
        self.assertIn("На проверке", mail.outbox[0].body)

    # ── Письмо не должно ломать регистрацию ──────────────────────────────────

    def test_registration_succeeds_when_smtp_is_down(self):
        with patch(
            "django.core.mail.EmailMultiAlternatives.send",
            side_effect=OSError("SMTP недоступен"),
        ):
            response = self._register()

        self.assertEqual(response.status_code, 201, response.content)
        # Клиент и его точка созданы, несмотря на упавшую почту.
        client = ClientProfile.objects.get(inn="5556667778")
        self.assertEqual(client.company_name, "ООО «Ромашка»")
        self.assertTrue(Store.objects.filter(client=client).exists())
        self.assertTrue(User.objects.filter(username="+79001112233").exists())

    def test_no_email_when_validation_fails(self):
        response = self._register(inn="123")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(mail.outbox, [])

    def test_no_email_on_duplicate_phone(self):
        self._register()
        mail.outbox = []

        response = self._register(inn="9998887776")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(mail.outbox, [])

    def test_no_email_on_duplicate_inn_in_region(self):
        self._register()
        mail.outbox = []

        response = self._register(username="+79005554433")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(mail.outbox, [])

    # ── Данные клиента не должны ломать вёрстку письма ───────────────────────

    def test_company_name_with_html_is_escaped(self):
        """Название компании подставляется в HTML — его нужно экранировать.

        Иначе кавычка или угловая скобка в названии ломает вёрстку письма, а
        `<img onerror=...>` превращает уведомление в вектор атаки на почту.
        """
        self._register(company_name='ООО <script>alert(1)</script> & "Ромашка"')

        html = mail.outbox[0].alternatives[0][0]
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&amp;", html)

    def test_contact_name_with_html_is_escaped(self):
        self._register(contact_name='Иван <b>Жирный</b>')

        html = mail.outbox[0].alternatives[0][0]
        self.assertNotIn("<b>Жирный</b>", html)
        self.assertIn("&lt;b&gt;", html)

    def test_html_layout_survives_quotes_in_name(self):
        self._register(company_name='ООО "Кавычки" & Ко')

        html = mail.outbox[0].alternatives[0][0]
        # Вёрстка не разъехалась: строка таблицы на месте и закрыта.
        self.assertIn("<strong>ООО &quot;Кавычки&quot; &amp; Ко</strong>", html)

    # ── Настройки и порядок ──────────────────────────────────────────────────

    @override_settings(
        EMAIL_BACKEND=LOCMEM,
        REGISTRATION_NOTIFICATION_EMAILS=["a@example.com", "b@example.com"],
    )
    def test_recipients_configurable(self):
        self._register()
        self.assertEqual(mail.outbox[0].to, ["a@example.com", "b@example.com"])

    def test_password_is_not_logged(self):
        with self.assertLogs("api.views", level="INFO") as logs:
            self._register(password="supersecret999")

        joined = "\n".join(logs.output)
        self.assertNotIn("supersecret999", joined)
        # При этом телефон и ИНН для диагностики остаются.
        self.assertIn("+79001112233", joined)


@override_settings(EMAIL_BACKEND=LOCMEM)
class RegistrationEmailTransactionTests(TransactionTestCase):
    """Отдельно от TestCase: тот сам оборачивает тест в транзакцию, и внутри
    него нельзя отличить транзакцию вьюхи от транзакции тестового окружения."""

    def setUp(self):
        self.http = Client()
        distributor = Distributor.objects.create(
            name="Дист", inn="1112223334", phone="1", email="d@e.co"
        )
        self.region = Region.objects.create(code="77", name="Москва", distributor=distributor)
        mail.outbox = []

    def test_email_sent_after_commit_not_inside_transaction(self):
        """Внутри транзакции SMTP держал бы её открытой до EMAIL_TIMEOUT, а при
        откате уведомление ушло бы по клиенту, которого в базе нет."""
        observed = {}

        def spy(message, *args, **kwargs):
            from django.db import transaction as tx

            observed["in_atomic_block"] = tx.get_connection().in_atomic_block
            return 1

        with patch("django.core.mail.EmailMultiAlternatives.send", spy):
            response = self.http.post(
                "/api/register/",
                data={
                    "username": "+79001112233",
                    "password": "verysecret123",
                    "inn": "5556667778",
                    "region_id": str(self.region.id),
                    "company_name": "ООО «Ромашка»",
                    "contact_name": "Иванов Иван",
                    "store_address": "Москва, пр. Мира 22",
                },
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 201, response.content)
        self.assertFalse(
            observed["in_atomic_block"],
            "письмо отправляется внутри открытой транзакции",
        )
