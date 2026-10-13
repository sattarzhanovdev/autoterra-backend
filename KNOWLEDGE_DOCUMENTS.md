# Документы базы знаний AutoTerra

PDF/DOCX/TXT (до 25 МиБ) загружаются через приложение по одному запросу на файл.
Эксперт управляет только своими документами, admin и superuser — всеми.
Остальные роли не имеют доступа к управлению или скачиванию оригиналов.
Содержимое готовых документов становится общей базой знаний для авторизованных
пользователей чата. Загружайте сюда материалы, предназначенные для ответов клиентам.
Карточки со статусом approved продолжают участвовать в поиске.

## Обработка

`KnowledgeDocument` хранит метаданные, статус, ошибку и состояние задания.
Оригиналы имеют случайные UUID-имена и хранятся в `PRIVATE_KNOWLEDGE_ROOT` вне
`MEDIA_ROOT`; API не возвращает путь или публичный URL. Приватный каталог и
файлы создаются с правами 0700/0600. Проверяются расширение, MIME и содержимое:
PDF signature, структура DOCX/OOXML, ограничения распаковки и текст TXT.
Отдельный upload handler прерывает чтение multipart при превышении лимита,
прежде чем большой файл заполнит временный каталог.
TXT поддерживает UTF-8, UTF-16 с BOM и Windows-1251. Старый бинарный DOC не поддерживается.

Команда `process_knowledge_documents` последовательно забирает задания из БД.
Обработка каждого файла идёт в отдельном subprocess с таймаутом 120 секунд,
ограничением CPU и памяти (1 ГиБ на Linux). После перезапуска worker забирает
задания с просроченной арендой; токен задания предотвращает запись результата
устаревшим worker. Ошибка одного файла не останавливает очередь.

Из PDF извлекается текст по страницам через pypdf. DOCX читается через безопасный
XML parser, включая таблицы и заголовки разделов. Номера страниц DOCX не вычисляются:
они зависят от программы, которой документ отображается. TXT — один раздел.
Пустые документы получают failed, пустые PDF — `ocr_required`. OCR ещё не реализован.
Зашифрованный PDF требует загрузки версии без пароля. Пределы: 2000 страниц,
5 млн символов, 50 МиБ распакованного DOCX, 10 МиБ потока одной страницы PDF.

Текст сохраняется в `KnowledgeChunk`: примерно 1400 символов, overlap 200,
исходный документ, номер страницы PDF или раздел DOCX. Публикация фрагментов и
статуса ready атомарна. Удаление удаляет фрагменты и оригинал после commit.

## Retrieval и чат

PostgreSQL использует `SearchVector`, `SearchQuery`, `SearchRank` с конфигурацией
russian и GIN-индексами (миграция 0059). SQLite использует ограниченный поиск
по словам; это локальный fallback без морфологии. Отдельная vector DB не нужна.
Функция `retrieve()` изолирует реализацию поиска: позже её можно заменить
embeddings/pgvector, сохранив документы, фрагменты и API.

В `/api/ai/chat/` передаются максимум 6 фрагментов, суммарно 8500 символов,
из готовых документов и approved-карточек. Огромный документ целиком не отправляется.
Hugging Face Router получает вопрос и только найденные выдержки; веса модели
не меняются. Модель задаётся HF_CHAT_MODEL, по умолчанию openai/gpt-oss-120b:groq.
HF_API_TOKEN хранится только в окружении backend. Без контекста модель не вызывается.
Промпт запрещает выполнять инструкции из документов, добавлять неизвестные
технические параметры и требует сообщать о недостатке данных. Это ограничение
генеративной модели, а не математическая гарантия отсутствия ошибок.
Без токена или при сбое провайдера API возвращает сами найденные выдержки.

Ответ сохраняет answer/suggestEscalation/sourceId для карточек и добавляет sources:
тип, ID, название, filename, page/section и ID фрагмента. Sources — предоставленные
модели материалы; они не означают независимую проверку всех утверждений ответа.
Flutter показывает источники под ответом. Содержимое документов и ответы провайдера
не записываются в логи обработки.

## API

- GET `/api/knowledge-documents/` — стандартная пагинация проекта.
- POST `/api/knowledge-documents/upload/` — multipart, ровно один `file`, optional `title`.
- GET/DELETE `/api/knowledge-documents/{id}/` — метаданные/удаление.
- POST `/api/knowledge-documents/{id}/reprocess/` — повторная обработка; 409, если уже processing.
- GET `/api/knowledge-documents/{id}/download/` — авторизованное скачивание, attachment/no-store.

Все запросы требуют `Authorization: Bearer ...`. Чужие ID эксперту дают 404.
Ошибка валидации загрузки — 400 с detail/code; ошибка извлечения сохраняется в
документе как failed с error/errorCode. Processing включает ожидание в очереди.
Flutter выбирает много файлов, отправляет их последовательно потоками, показывает
прогресс чтения/отправки и ожидание ответа сервера, затем результаты каждого файла.
Ошибки не прерывают оставшиеся загрузки. Видимые processing-строки обновляются
каждые 5 секунд; доступны ручное обновление, удаление и повтор обработки.
Admin открывает базу знаний с панели администратора, эксперт — из своего раздела.

## Production: команды для ручного деплоя

Ниже шаблон для Linux/systemd. Замените путь, пользователя и имя существующего
backend-сервиса реальными значениями. Эти команды автоматически не выполнялись.
Сначала перенесите проверенные изменения обоих репозиториев на сервер обычным
процессом доставки кода и сделайте резервную копию БД.

```bash
cd /srv/autoterra-backend
.venv/bin/python -m pip install -r requirements.txt
sudo install -d -m 0700 -o autoterra -g autoterra /var/lib/autoterra/knowledge
```

В серверном `.env` задайте:

```dotenv
PRIVATE_KNOWLEDGE_ROOT=/var/lib/autoterra/knowledge
HF_CHAT_MODEL=openai/gpt-oss-120b:groq
# HF_API_TOKEN=<секрет только на сервере>
```

Backend и worker должны работать под пользователем, имеющим доступ к одному
приватному каталогу. Не добавляйте для него nginx alias или публичную раздачу.
Существующий `.env` должен оставаться приватным и не попадать в Flutter assets.
Если контейнеры — подключите один persistent volume к обоим процессам.

В существующем nginx server/location для API задайте `client_max_body_size 27m;`
(запас на multipart). Затем:

```bash
.venv/bin/python manage.py migrate
.venv/bin/python manage.py check
.venv/bin/python manage.py collectstatic --noinput
# Сначала адаптируйте deploy/autoterra-knowledge-worker.service к вашим путям.
sudo cp deploy/autoterra-knowledge-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now autoterra-knowledge-worker
sudo systemctl restart <имя-backend-сервиса>
sudo nginx -t
sudo systemctl reload nginx
sudo systemctl status autoterra-knowledge-worker
```

Для проверки очереди без постоянного сервиса:

```bash
.venv/bin/python manage.py process_knowledge_documents --once
```

`--once` обрабатывает все доступные задания и выходит, когда очередь пуста.
На большой существующей таблице карточек создание GIN-индекса в миграции может
потребовать окно обслуживания. Резервное копирование включает БД и приватный каталог.

Сборка Flutter, из репозитория autoterra:

```bash
flutter pub get
flutter test
flutter analyze
flutter build appbundle --release --dart-define=API_BASE_URL=https://autoterra.shop/api/
# Для веб-версии:
flutter build web --release --dart-define=API_BASE_URL=https://autoterra.shop/api/
```

Публикация сборок выполняется отдельно стандартным способом проекта.

## Проверки локально

```bash
DEBUG=True DB_ENGINE=sqlite3 HF_API_TOKEN='' .venv/bin/python manage.py test api --noinput
DEBUG=True DB_ENGINE=sqlite3 .venv/bin/python manage.py check
DEBUG=True DB_ENGINE=sqlite3 .venv/bin/python manage.py makemigrations --check --dry-run
```

Тесты Hugging Face используют mock и проверяют фактическое содержимое запроса.
Production-токен и реальные документы в этих проверках не используются.
Дополнительно тесты документов выполняются на отдельной локальной PostgreSQL-базе.

## Изменённые файлы

Backend (`autoterra-backend/`):

- `.env.example`, `.gitignore`, `requirements.txt`
- `autoterra_backend/settings.py`
- `api/apps.py`, `api/models.py`, `api/urls.py`, `api/views.py`
- `api/knowledge_views.py`
- `api/services/knowledge_documents.py`, `api/services/knowledge_retrieval.py`
- `api/management/commands/process_knowledge_documents.py`
- `api/migrations/0058_knowledgedocument_knowledgechunk.py`
- `api/migrations/0059_knowledge_search_indexes.py`
- `api/test_knowledge_documents.py`
- `deploy/autoterra-knowledge-worker.service`
- `KNOWLEDGE_DOCUMENTS.md`

Flutter (`autoterra/`):

- `lib/services/api_client.dart`, `lib/services/knowledge_upload_queue.dart`
- `lib/screens/expert/expert_knowledge_base_screen.dart`
- `lib/screens/expert/knowledge_documents_panel.dart`
- `lib/screens/ai/ai_assistant_screen.dart`
- `lib/screens/admin/admin_dashboard_screen.dart`
- `test/knowledge_documents_test.dart`

## Результаты проверки реализации (13 октября 2026)

- Полный backend: 679 tests, OK, 5 skipped (включая PostgreSQL-only проверки на SQLite).
- Документы на PostgreSQL 16: 21 tests, OK; проверены морфология и использование GIN-индексов через EXPLAIN.
- Реальный worker subprocess: PDF, DOCX, пустой TXT, обычный TXT → ready, ready, failed, ready; приватные файлы удалены вместе с документами.
- Flutter: 172 tests, all passed; flutter analyze — no issues.
- manage.py check — no issues; makemigrations --check --dry-run — no changes detected.
- Тестовые PostgreSQL-базы и временные файлы удалены. Production не изменялся, push не выполнялся.
- Hugging Face проверен через mock; реальный Router в тестах не вызывался.

Использованные API описаны в [документации Django по полнотекстовому поиску](https://docs.djangoproject.com/en/5.2/ref/contrib/postgres/search/)
и [документации pypdf по извлечению текста](https://pypdf.readthedocs.io/en/stable/user/extract-text.html).
