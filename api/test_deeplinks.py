"""Проверка владения доменом для ссылок-приглашений.

Android открывает https-ссылку в приложении только после того, как скачает
/.well-known/assetlinks.json и найдёт там отпечаток подписи APK. Ошибка здесь
не видна пользователю — ссылка просто уходит в браузер, поэтому формат файла
проверяем тестами.
"""

from django.test import Client, TestCase, override_settings

FINGERPRINT = "AB:CD:" + ":".join(["00"] * 30)


class AssetLinksTests(TestCase):
    def setUp(self):
        self.http = Client()

    def _fetch(self):
        return self.http.get("/.well-known/assetlinks.json")

    @override_settings(
        ANDROID_APP_PACKAGE="shop.autoterra.app",
        ANDROID_APP_FINGERPRINTS=[FINGERPRINT],
    )
    def test_returns_declaration_for_the_app(self):
        response = self._fetch()
        self.assertEqual(response.status_code, 200)

        entry = response.json()[0]
        self.assertEqual(entry["relation"], ["delegate_permission/common.handle_all_urls"])
        self.assertEqual(entry["target"]["namespace"], "android_app")
        self.assertEqual(entry["target"]["package_name"], "shop.autoterra.app")
        self.assertEqual(entry["target"]["sha256_cert_fingerprints"], [FINGERPRINT])

    @override_settings(
        ANDROID_APP_PACKAGE="shop.autoterra.app",
        ANDROID_APP_FINGERPRINTS=[FINGERPRINT],
    )
    def test_body_is_a_json_array(self):
        # Android ждёт именно массив: объект он молча не примет.
        self.assertIsInstance(self._fetch().json(), list)

    @override_settings(ANDROID_APP_FINGERPRINTS=[])
    def test_missing_fingerprint_is_an_explicit_failure(self):
        """Пустой список тише всего ломает deep links — отвечаем ошибкой."""
        response = self._fetch()

        self.assertEqual(response.status_code, 503)

    @override_settings(
        ANDROID_APP_PACKAGE="shop.autoterra.app",
        ANDROID_APP_FINGERPRINTS=[FINGERPRINT, "EF:01:" + ":".join(["11"] * 30)],
    )
    def test_supports_several_signing_keys(self):
        # Ключ загрузки и ключ подписи Google Play — разные отпечатки, и оба
        # должны попасть в файл, иначе ссылки сломаются у части установок.
        entry = self._fetch().json()[0]

        self.assertEqual(len(entry["target"]["sha256_cert_fingerprints"]), 2)
