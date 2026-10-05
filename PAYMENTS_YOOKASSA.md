# Оплата АвтоТерра через ЮKassa

Приложение использует единую размещённую страницу оплаты ЮKassa (redirect).
Карты, ЮMoney, СБП и другие доступные способы показывает сама ЮKassa — набор
зависит от подключения магазина. Секретные реквизиты в APK не передаются.
Полностью покрытый бонусами заказ закрывается без денежного платежа.
Загрузка чека о прошлой закупке остаётся учётом закупки, а не новым списанием.

## Ключи

В личном кабинете https://yookassa.ru/my выберите нужный магазин.
В разделе «Интеграция → Ключи API» получите реквизиты этого же магазина:

| Параметр | Что указать | Где хранить |
|---|---|---|
| `YOOKASSA_SHOP_ID` | shopId, идентификатор магазина, не номер кошелька ЮMoney | `.env` бэкенда |
| `YOOKASSA_SECRET_KEY` | Секретный ключ API выбранного магазина | Только `.env` бэкенда / серверное хранилище секретов |
| `YOOKASSA_MOBILE_SDK_KEY` | Не требуется, оставить пустым | В этом сценарии не используется |
| `YOOKASSA_RETURN_URL` | `https://autoterra.shop/api/payments/yookassa/return/` | `.env` бэкенда |
| `YOOKASSA_RECEIPT_EMAIL` | Действующий адрес для чека, если у клиента нет email | `.env` бэкенда |

Сначала используйте тестовый магазин и его ключ. После проверки замените ОБА
реквизита на реквизиты рабочего магазина. Не смешивайте тестовый shopId с боевым
ключом. Ключ SDK понадобится только при отдельном переходе на нативный мобильный
SDK; это другой сценарий, сейчас его не нужно генерировать.

Официальные инструкции: [ключ API](https://yookassa.ru/docs/support/merchant/payments/implement/keys),
[ключ SDK](https://yookassa.ru/docs/support/merchant/payments/implement/keys-app),
[платежи](https://yookassa.ru/developers/payment-acceptance/getting-started/quick-start).

## Сервер

1. Сделайте резервную копию БД, загрузите обновлённый код бэкенда. Не заменяйте
   существующий `.env` примером и не переносите ключи в `autoterra/.env`:
   последний файл включается в APK как Flutter asset.
2. В существующий `.env` бэкенда добавьте:

   ```dotenv
   YOOKASSA_SHOP_ID=идентификатор_вашего_магазина
   YOOKASSA_SECRET_KEY=секретный_ключ_этого_магазина
   YOOKASSA_RECEIPT_EMAIL=receipts@example.com
   YOOKASSA_RETURN_URL=https://autoterra.shop/api/payments/yookassa/return/
   ```

   Если API размещён на другом домене, замените домен здесь, в webhook и при
   сборке APK. Сохраните существующие серверные настройки `SECRET_KEY`,
   `ALLOWED_HOSTS` и прочие; для боевого сервера установите `DEBUG=False`.
   Переменные окружения systemd/Docker имеют приоритет над `.env`: обновите их,
   если ключи уже задаются там.

3. Выполните из каталога бэкенда в используемом сервером окружении Python 3.10+:

   ```bash
   .venv/bin/python -m pip install -r requirements.txt
   .venv/bin/python manage.py check
   .venv/bin/python manage.py migrate
   .venv/bin/python manage.py collectstatic --noinput
   ```

   Если окружения ещё нет, сначала создайте его: `python3.12 -m venv .venv`.
   Перезапустите действующий процесс Django/Gunicorn или контейнер. Для systemd:
   `sudo systemctl restart ИМЯ_ВАШЕГО_СЕРВИСА`. Имя сервиса не хранится в этом
   репозитории; подставьте имя вашего развёртывания. Не запускайте второй
   `runserver` вместо перезапуска боевого приложения.

4. В кабинете ЮKassa настройте HTTP-уведомления на:

   ```text
   https://autoterra.shop/api/payments/yookassa/webhook/
   ```

   События: `payment.succeeded`, `payment.canceled`, `payment.waiting_for_capture`.
   URL должен принимать POST по HTTPS без перенаправления, входа в аккаунт и
   CAPTCHA/Cloudflare challenge. Django сам перепроверяет объект через API
   ЮKassa; дополнительный `YOOKASSA_WEBHOOK_SECRET` не используется.
   [Документация уведомлений](https://yookassa.ru/developers/using-api/webhooks).

5. Добавьте фоновую сверку каждую минуту. Пример crontab с абсолютными путями:

   ```cron
   * * * * * cd /srv/autoterra-backend && /usr/bin/flock -n /srv/autoterra-backend/reconcile.lock /srv/autoterra-backend/.venv/bin/python manage.py reconcile_payments --limit 100 >> /srv/autoterra-backend/reconcile-payments.log 2>&1
   ```

   Замените `/srv/autoterra-backend` фактическим путём. Задачу запускайте от
   пользователя приложения с доступом к БД и `.env`; при хранении реквизитов
   только в окружении systemd используйте systemd timer с тем же EnvironmentFile.
   Настройте ротацию журнала и контроль ненулевого кода завершения. Команда
   проверяет статусы и восстанавливает попытки с потерянным ответом на создание.
   Размер пакета подберите под нагрузку. Для больших объёмов нужна очередь задач.

6. Запрос создания платежа содержит товарный чек со ставкой НДС 5% (`vat_code=7`).
   Email берётся из профиля клиента или `YOOKASSA_RECEIPT_EMAIL`. Сумма позиций
   равна сумме платежа после применения бонусов. Укажите действующий резервный
   email и проверьте настройки чеков вашего магазина в ЮKassa.
   [Чеки в ЮKassa](https://yookassa.ru/developers/payment-acceptance/receipts/54fz/other-services/payments).

## Как проверять перед запуском

В тестовом магазине: создайте заказ, подтвердите его оператором, откройте оплату
из списка закупок и из карточки заказа. Проверьте успешную оплату, отмену,
частичную/полную оплату бонусами, повторное нажатие, выход из приложения и
возвращение. Полный бонус не должен открывать платёжную страницу.

Успех должен появиться только после подтверждения API ЮKassa. Переход на return
URL сам по себе заказ не оплачивает. Вернитесь в приложение: оно запрашивает
актуальный статус. Можно также обновить карточку вручную. Для проверки задержки
уведомления используйте команду `manage.py reconcile_payments`.

Платёж с сетевой ошибкой остаётся в ожидании: бонусы зарезервированы, повторная
попытка использует тот же сохранённый запрос и Idempotence-Key. Не удаляйте такую
запись и не переводите её вручную в canceled без сверки с ЮKassa. Если ID ответа
не получен за 23 часа, автоматическое повторное создание блокируется, чтобы
не выйти за срок действия идемпотентности провайдера. Оператор должен найти
платёж в ЮKassa по заказу/сумме/времени и сверить результат; если платёж существует,
его ID можно привязать к той же записи Payment и запустить сверку. Возвращать
бонусы и разрешать новую попытку можно только после достоверной отмены/отсутствия
списания. [Идемпотентность](https://yookassa.ru/developers/using-api/interaction-format).

SQLite настроен на IMMEDIATE-транзакции с ожиданием блокировки до 30 секунд
(Django 5.2 из requirements). Вызовы API выполняются вне транзакции резервирования,
чтобы не держать БД заблокированной во время сетевого ожидания.

## APK

На машине сборки нужны Flutter, Android SDK, JDK и действующий ключ подписи.
В проекте уже настроен `android/key.properties`. Сохраняйте существующий ключ:
обновления установленного APK должны быть подписаны совместимым сертификатом.

Если приложение собирается впервые и ключа действительно нет:

```bash
mkdir -p "$HOME/keys"
keytool -genkeypair -v -keystore "$HOME/keys/autoterra-upload.jks" -storetype JKS -keyalg RSA -keysize 2048 -validity 10000 -alias upload
```

Создайте `autoterra/android/key.properties` (пароли не коммитить):

```properties
storePassword=пароль_хранилища
keyPassword=пароль_ключа
keyAlias=upload
storeFile=/абсолютный/путь/autoterra-upload.jks
```

Из каталога Flutter-приложения `autoterra`:

```bash
flutter pub get
flutter analyze lib/screens/orders/order_detail_screen.dart lib/services/data_repository.dart lib/models/models.dart
flutter test test/payment_start_test.dart test/order_config_parsing_test.dart test/order_highlight_test.dart
flutter build apk --release --dart-define=API_BASE_URL=https://autoterra.shop/api/
```

Готовый универсальный APK: `build/app/outputs/flutter-apk/app-release.apk`.
Для обновления увеличьте номер после `+` в `pubspec.yaml` или задайте новый
`--build-number`. Для Google Play обычно собирается AAB:

```bash
flutter build appbundle --release --dart-define=API_BASE_URL=https://autoterra.shop/api/
```

[Официальная инструкция Flutter](https://docs.flutter.dev/deployment/android).
Перед распространением установите APK на реальный телефон и пройдите тестовую
оплату. Серверные ключи в команду сборки не добавляются.

## Первая проверка (до переноса ключа подписи)

- 77 серверных тестов платежей, заказов и бонусов прошли.
- 9 тестов Flutter прошли; анализ изменённых Dart-файлов не сообщил ошибок.
- Проверка миграций: изменений схемы БД нет.
- В полном наборе отдельно воспроизводятся на исходном коде 4 падения тестов
  доступа курьеров/дистрибьюторов и отсутствие `python-docx` для теста экспорта.
- Release-сборка на текущем компьютере дошла до подписи и остановилась:
  `android/key.properties` ссылается на отсутствующий файл
  `/Users/adminbaike/keys/autoterra-upload.jks`. Перенесите прежний ключ и исправьте
  `storeFile` на его реальный абсолютный путь. Не используйте ключ другого проекта.
- Реальный платёж не проводился: реквизиты магазина ещё не заданы. Сервер не
  развёртывался. Требуется проверка тестового магазина и согласование схемы чеков.

Дополнительная debug-сборка остановилась с `No space left on device`. Перед
повторной сборкой освободите место для Android/Gradle и промежуточных файлов.
Неудачные debug-артефакты этой проверки удалены. Готовый APK не создан.

## Обновление после переноса ключа

Ключ добавлен в `autoterra/android/app/autoterra-upload.jks`, относительный путь
в `android/key.properties` исправлен. Release APK успешно собран и подпись
проверена на соответствие этому ключу. Для повторения используйте
`./scripts/build_rustore_apk.sh` из каталога `autoterra`; подробности —
`autoterra/BUILD_RUSTORE.md`. Серверные ключи и схема чеков по-прежнему требуют
отдельной настройки.
