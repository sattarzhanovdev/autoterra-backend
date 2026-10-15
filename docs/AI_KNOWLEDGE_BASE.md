# AI: история, проверка ответов и база знаний

## Архитектура

Существующий `POST /api/ai/chat/` и Hugging Face Router / `HF_CHAT_MODEL`
сохранены. Groq выбирается существующим суффиксом модели `:groq`, новый AI API
не добавляется. `ExpertTicket` остаётся отдельным обращением к человеку.

```text
Папка Google Drive → sync_google_drive_knowledge → приватный KnowledgeDocument
  → ограниченный parser subprocess → KnowledgeChunk
                                      ↓
approved KnowledgeCard ───────────→ retrieve → существующая LLM → ответ
  ↑                                                    ↓
явное одобрение сотрудника ← AIMessageFeedback ← AIMessage / AIConversation
```

Миграция `0061_ai_history_drive_knowledge` добавляет четыре таблицы:
`AIConversation`, `AIMessage`, `AIMessageFeedback`, `KnowledgeDriveFile`.
В `KnowledgeDocument` добавлены `active`, `source_type`, `source_url`, в
`KnowledgeChunk` — `sheet`, `row_start`, `row_end`. Существующие документы
остаются активными. Данные `ExpertTicket` и `KnowledgeCard` не переносятся.

## История и API

Каждый авторизованный вызов чата с допустимым ID своего диалога сохраняет
исходный вопрос и отдельную запись ответа. Это включает пустые вопросы,
неверный тип message и превышение 4000 символов; HTTP 200/400 сохраняют прежние
основные поля. Неавторизованные запросы и попытки доступа к чужому диалогу
отвергаются до записи. Некорректный JSON отклоняется существующим middleware.

Сообщение пользователя и placeholder ответа `pending` фиксируются **до** поиска
и вызова LLM. Сетевой запрос не удерживает транзакцию БД. У ответа сохраняются
оригинальный текст, статус, безопасный код ошибки, latency в миллисекундах,
провайдер/модель (если вызов был), insufficient и снимки источников с выдержками.
Ошибка провайдера сохраняет существующий fallback с выдержками и HTTP 200.
Непредвиденная ошибка поиска сохраняет ответ `error` и возвращает HTTP 503.
Если процесс принудительно завершён, `pending` остаётся виден сотруднику:
это незавершённый запрос, а не потерянный вопрос. Недоступность самой БД
не позволяет гарантировать запись; перед запуском backend обязательна миграция.

Старое тело запроса продолжает работать:

```json
{"message":"Как сушить грунт?"}
```

К ответу добавлены необязательные строковые `conversationId` и `messageId`.
Чтобы следующие сообщения появились в одном диалоге, клиент может передавать:

```json
{"message":"Следующий вопрос", "conversationId":"123"}
```

ID проверяется по владельцу. Без conversationId каждый запрос создаёт отдельный
диалог: прежний мобильный клиент сохраняет все обращения без обновления сборки,
но для группировки нескольких обращений экрану нужно запоминать conversationId.
Старая история до внедрения отсутствует и не восстанавливается из тикетов.
Группировка — для аудита; прежние реплики сейчас не добавляются в prompt,
поведение поиска и контекст модели не меняются. Endpoint чтения истории для
мобильного клиента в этот объём не входит: история предназначена сотрудникам.

История следует жизненному циклу User: при удалении аккаунта диалоги удаляются.
Снимок связи с клиентом nullable; удаление ClientProfile не удаляет диалог.
Повторная отправка HTTP-запроса считается отдельным обращением, не скрывается
дедупликацией. Для большого объёма нужно отдельно определить срок хранения,
резервное копирование и политику удаления истории.

## Django Admin и проверка

Раздел **История ИИ** показывает пользователя, клиента, дату, количество сообщений,
последний вопрос и состояние проверки. Поиск работает по клиенту, пользователю,
вопросам и ответам. Фильтры: дата, клиент, проверка и оценка. На странице диалога
сообщения следуют по порядку создания; доступны источники и ссылка проверки.

Оригиналы сообщений доступны только для чтения. В **Проверки ответов ИИ**:

- Не проверено / Ответ правильный / Ответ неправильный;
- Правильный ответ и комментарий;
- кто и когда сохранил проверку;
- отдельный флаг «Одобрить для базы знаний».

Оценка и исправление сами по себе **не участвуют** в retrieval. При явном
одобрении создаётся одна approved `KnowledgeCard` с вопросом и исправленным
ответом; для оценки «правильный» можно использовать оригинальный ответ без
исправления. Повторное сохранение обновляет ту же карточку. Снятие одобрения
архивирует её. Оригинальный ответ ИИ никогда не переписывается. Это RAG, не
fine-tuning. Для неверного ответа публикация без исправления запрещена.

Доступ требует `is_staff` и стандартных Django model permissions. При настройке
группы сотрудников выдайте:

- view AIConversation, view AIMessage;
- view/add/change AIMessageFeedback;
- для публикации — add/change KnowledgeCard (и view для просмотра карточек);
- view KnowledgeDriveFile / KnowledgeDocument для состояния документов.

Роль приложения `ai_expert` сама по себе не выдаёт доступ в Django Admin.
Отдельный API feedback не открыт. Опубликованные материалы и история содержат
пользовательские вопросы: доступ к этим данным предоставляется осознанно.

## Google Cloud и доступ к папке

1. Создайте/выберите Google Cloud project, включите **Google Drive API**.
2. IAM & Admin → Service Accounts → Create service account. Проектные роли
   Owner/Editor и domain-wide delegation для чтения выделенной папки не нужны.
3. Создайте JSON-ключ этого service account. Из JSON возьмите `client_email`
   вида `autoterra-knowledge@PROJECT_ID.iam.gserviceaccount.com`.
4. В Google Drive откройте папку:
   https://drive.google.com/drive/folders/1mUIX2or26vC23eSw_ej9ZRf_AkZfAkaI
   и добавьте **именно этот email** через «Поделиться» с ролью **Читатель / Viewer**.
   Убедитесь, что наследование доступа позволяет читать вложенные файлы.
   Публичный доступ «всем по ссылке» не требуется.
5. Перенесите ключ на backend сервер вне репозитория, например
   `/etc/autoterra/google-drive-service-account.json`. Владелец — пользователь
   backend, права 0600, родительский каталог с ограниченным доступом.
   Не используйте Firebase service account автоматически.

Код запрашивает только scope `https://www.googleapis.com/auth/drive.readonly`.
В Drive не выполняются создание, изменение, удаление файлов или изменение прав.
Если политика Workspace запрещает внешний service account, администратор
Workspace должен разрешить доступ к выделенной папке; не расширяйте права на
весь домен в качестве обходного решения.

Официальные справочники:
- https://developers.google.com/workspace/guides/create-credentials
- https://developers.google.com/workspace/drive/api/guides/manage-downloads
- https://developers.google.com/workspace/drive/api/guides/ref-export-formats

## Environment

В `.env.example` есть безопасные примеры. На сервере задайте:

```dotenv
GOOGLE_DRIVE_KNOWLEDGE_FOLDER_ID=1mUIX2or26vC23eSw_ej9ZRf_AkZfAkaI
GOOGLE_DRIVE_CREDENTIALS_FILE=/etc/autoterra/google-drive-service-account.json
GOOGLE_DRIVE_REQUEST_TIMEOUT=30
GOOGLE_DRIVE_DOWNLOAD_TIMEOUT=120
GOOGLE_DRIVE_MAX_FILES=10000
PRIVATE_KNOWLEDGE_ROOT=/var/lib/autoterra/knowledge
```

Существующие `HF_API_TOKEN`, `HF_CHAT_MODEL`, `HF_CHAT_URL`, `HF_CHAT_TIMEOUT`
сохраняются. Ключи HF/Google не передаются мобильному приложению. Библиотеки
`google-auth>=2.29,<3` и `requests>=2.32.3,<3` теперь явно указаны в requirements;
они обычно уже установлены транзитивно через Firebase. Нового платного AI,
Celery, Redis или pgvector нет.

## Синхронизация

```bash
.venv/bin/python manage.py sync_google_drive_knowledge
# Повторить неизменившиеся файлы со статусом error/unsupported после исправления:
.venv/bin/python manage.py sync_google_drive_knowledge --retry-failed
```

Обход рекурсивный с пагинацией, поддерживает обычные и shared-drive папки.
Shortcuts не разыменовываются и помечаются unsupported. Сначала собирается
полный inventory; incompleteSearch, повтор page token, превышение лимита
inventory или ошибка listing прерывают запуск без массового удаления.

Уникальный file_id предотвращает дубли. Fingerprint включает metadata Drive
(имя, MIME, modifiedTime, version/checksum, size). При полном совпадении запись,
last_synced_at и chunks **не меняются**, скачивание не выполняется.
last_synced_at — время последней попытки применить версию, не каждого poll.
Неизменившиеся ошибки требуют `--retry-failed` либо изменения версии в Drive.

Изменённый документ сначала исключается из поиска, затем новая версия скачивается
в ограниченный временный файл и разбирается в subprocess. Новый ready-документ
публикуется атомарно, старый документ/фрагменты удаляются. При ошибке старый текст
остаётся неактивным: AI не использует заведомо устаревшую версию. Удалённые,
перемещённые за пределы дерева и исчезнувшие из доступного inventory файлы
деактивируются вместе с документом; их chunks не участвуют в retrieval.
При возвращении файла он обрабатывается снова.

Документы из этой папки **автоматически участвуют в общей базе ответов после
успешной обработки**. Держите в ней только материалы, разрешённые для ответов
клиентам. Это не зеркало произвольного личного Drive. Индивидуальные Drive ACL
не переносятся в доступ к тексту через AI. Исходные файлы не раздаются публично.

При потере доступа ко всей корневой папке listing завершится ошибкой и сохранит
последнюю локальную базу. Для срочного отзыва всей базы остановите sync и через
Django shell выполните:

```python
from api.models import KnowledgeDocument, KnowledgeDriveFile
KnowledgeDocument.objects.filter(source_type='google_drive').update(active=False)
KnowledgeDriveFile.objects.all().update(active=False, status='inactive')
```

Это также нужно сделать перед сменой корневой папки: источники старой папки не
удаляются автоматически запуском sync для другой папки.

Блокировка: PostgreSQL advisory lock защищает от одновременных sync, SQLite —
flock в PRIVATE_KNOWLEDGE_ROOT. Backend/worker/sync должны иметь общий private
volume. Обычный worker загрузок не забирает уже арендованный Drive parser job.

В конце выводится JSON: found, created, updated, unchanged, deactivated, failed.
Ошибки/unsupported одного файла не останавливают остальные. После вывода
статистики команда возвращает ненулевой exit code при failed > 0, чтобы
systemd/cron заметил проблему. Для файловых ошибок выводятся безопасные коды,
без содержимого документов, ключей и ответов Google. В Django Admin раздел
**База знаний Google Drive** показывает статус, ошибку, дату, chunks и оригинал.
Долгая синхронизация из HTTP admin не запускается.

## Форматы и ограничения

- PDF: pypdf, страница сохраняется. Сканы без текстового слоя → ocr_required;
  зашифрованные PDF не поддерживаются.
- DOCX: безопасный XML, логические блоки/заголовки; номера страниц не вычисляются.
- XLSX: openpyxl read-only/data-only, без выполнения формул, макросов и внешних
  ссылок; sheet и диапазон строк сохраняются. Формулы без cached values могут
  не дать текста. Предел 256 столбцов и 100000 строк на лист.
- TXT: UTF-8, UTF-16 BOM, Windows-1251.
- HTML: только текст HTMLParser; script/style/iframe/object/template/svg скрыты,
  JS не выполняется, внешние ресурсы не скачиваются. UTF-8/UTF-16/CP1251.
- Google Docs экспортируются в DOCX, Google Sheets — в XLSX. Ограничения export
  Drive API применяются отдельно от нашего лимита (обычный files.export: 10 MB).
- MP4/audio → unsupported / transcription_required. TODO: отдельная проверенная
  транскрибация, платные/тяжёлые сервисы не подключены. Другие форматы unsupported.

Лимиты существующего parser: файл до 25 МиБ, 120 секунд, CPU limit, на Linux
1 ГиБ памяти, до 5 млн извлечённых символов, до 2000 страниц PDF, ограничение
распаковки Office 50 МиБ. XLSX и HTML доступны также существующей загрузке
документов API; старые форматы и endpoints сохранены.

## Retrieval и источники

`api/services/knowledge_retrieval.py` остаётся одной точкой поиска. PostgreSQL:
русские SearchVector/SearchQuery/SearchRank, существующие GIN indexes (0059).
SQLite: ограниченный icontains fallback. В модель передаются до 6 фрагментов,
8500 символов. Источники — активные ready-документы и approved-карточки, включая
явно опубликованные проверки сотрудников. Embeddings не используются.

System prompt требует русский ответ только по контексту, запрещает выполнять
инструкции из материалов и придумывать технические характеристики. Без контекста
LLM не вызывается, возвращается недостаточность данных и предложение эксперта
для клиентских ролей. Это ограничение поведения, не гарантия точности модели.

`sources` сохраняют прежние поля и добавляют URL, score, sourceType,
sheet/rowStart/rowEnd для документов. В AIMessage дополнительно сохраняется
`excerpt`: точная выдержка, предоставленная модели/показанная fallback. Это
позволяет проверить старый ответ после изменения/удаления исходного документа.
Источники перечисляют предоставленный контекст, а не доказанную проверку каждого
утверждения. Доступ к оригиналам остаётся под существующей авторизацией API
или ACL Google Drive; наличие URL не делает документ публичным.

## Автосинхронизация

В `deploy/` добавлены `autoterra-drive-knowledge.service` и
`autoterra-drive-knowledge.timer` (каждые 15 минут). Сначала замените пути и
пользователя под сервер. Таймер не запускается автоматически из репозитория.
Периодическая задача не требует постоянного process_knowledge_documents worker
для Drive, поскольку запускает тот же bounded parser сама; существующий worker
нужен для ручных загрузок приложения.

## Production deployment

Ниже команды для `/srv/autoterra-backend`, пользователя `autoterra` и systemd
backend `autoterra-backend`. Замените эти три значения, если реальные отличаются.
Сделайте резервную копию PostgreSQL и PRIVATE_KNOWLEDGE_ROOT перед миграцией.
Не запускайте migrate до backup; на нескольких backend instances сначала
координируйте остановку старых worker/backend.

```bash
cd /srv/autoterra-backend
git switch adilhan_new
git pull --ff-only origin adilhan_new
.venv/bin/python -m pip install -r requirements.txt
# Создайте service account и установите credentials/env по инструкции выше.
sudo install -d -m 0700 -o autoterra -g autoterra /var/lib/autoterra/knowledge
sudo chown autoterra:autoterra /etc/autoterra/google-drive-service-account.json
sudo chmod 0600 /etc/autoterra/google-drive-service-account.json
.venv/bin/python manage.py check
.venv/bin/python manage.py migrate
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py migrate --check
.venv/bin/python manage.py collectstatic --noinput
sudo systemctl restart autoterra-backend
sudo systemctl restart autoterra-knowledge-worker
sudo -u autoterra .venv/bin/python manage.py sync_google_drive_knowledge
# После проверки состояния файлов в Django Admin:
sudo cp deploy/autoterra-drive-knowledge.service /etc/systemd/system/
sudo cp deploy/autoterra-drive-knowledge.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now autoterra-drive-knowledge.timer
systemctl list-timers autoterra-drive-knowledge.timer
```

Production автоматически не менялся. Первый реальный sync возможен только после
предоставления server credentials и доступа service account к папке.

## Проверки и troubleshooting

```bash
# Полный suite, локальная SQLite (не production):
DEBUG=True DB_ENGINE=sqlite3 DB_NAME=:memory: HF_API_TOKEN='' \
  EMAIL_BACKEND=django.core.mail.backends.locmem.EmailBackend \
  .venv/bin/python manage.py test --noinput
# PostgreSQL нужен для проверки русской морфологии и GIN-планов.
```

Все новые тесты Drive и LLM используют mocks, не реальные Google API/credentials.
Отдельно покрыты история, источники, provider errors, permissions, feedback,
публикация/отзыв, новый/изменённый/удалённый файл, идемпотентность, сломанный PDF,
unsupported, HTML, XLSX, PDF page, экспорт и пагинация.

- credentials_not_configured / invalid_credentials_file: путь, права и JSON ключ.
- drive_http_403: Drive API включён, папка расшарена на client_email как Viewer,
  download/export разрешены политикой Workspace.
- drive_http_404: неверный ID или нет доступа. Полный listing не деактивирует базу
  при ошибке: проверьте настройки, затем повторите.
- sync_already_running: другой процесс держит lock; не запускайте параллельно.
- inventory_limit: проверьте дерево папок и GOOGLE_DRIVE_MAX_FILES.
- file_too_large / too_large: уменьшите документ или разбейте его.
- ocr_required / processing_limit: подготовьте текстовый PDF / упростите файл.
- unsupported: поддерживаемый экспорт/формат или отдельная транскрибация видео.
- error после исправления файла без изменения metadata: --retry-failed.
- sources пусты: проверьте active/ready, approved карточки и слова запроса.
- provider_error: HF token, модель, доступность Router; вопрос/выдержки сохранены.
- pending в истории: процесс завершился до ответа; проверьте backend timeout/logs.
- нет истории в админке: is_staff, view permissions и применение миграции 0061.
