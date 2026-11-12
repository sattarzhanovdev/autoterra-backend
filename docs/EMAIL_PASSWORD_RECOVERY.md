# Email password recovery

This API resets **Django User accounts only**. It does not change passwords in
Bitrix or synchronize separate account databases. Before connecting the public
site, verify that its login uses these accounts and that `User.email` contains
the customer's previously registered address. The API does not attach a newly
provided address to an account. Ambiguous duplicate active emails are skipped;
correct those accounts through the administrator.

### Server setup

1. Preserve the server's existing SMTP configuration and secrets. Recovery reuses
   the existing `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`,
   `EMAIL_USE_TLS`, `EMAIL_USE_SSL`, `EMAIL_BACKEND`, and `DEFAULT_FROM_EMAIL` from
   environment variables or `.env`. No separate mail service is needed.
   TLS and SSL must not both be enabled. Keep a stable, random
   `SECRET_KEY` in production; changing it invalidates Django sessions.
2. Apply migrations: `.venv/bin/python manage.py migrate`.
3. Set `PASSWORD_RESET_URL` to the final **HTTPS frontend reset page**, e.g.
   `https://avtoterraplus.ru/reset-password.html`, without query or fragment.
   Requests return 503 until this is configured. Do not enable public recovery
   until that page is implemented. There is no frontend change in this revision.
4. Schedule `.venv/bin/python manage.py send_password_reset_emails --limit 100`
   every minute from the backend directory using the server's cron/systemd.
   Systemd examples are in `deploy/autoterra-password-reset-emails.service` and
   `.timer`; adjust the installation path/user before enabling the timer.
   For example, replace `/absolute/path/to/backend` with the deployment directory:

   ```cron
   * * * * * cd /absolute/path/to/backend && .venv/bin/python manage.py send_password_reset_emails --limit 100
   ```

The request endpoint only queues mail. Without the scheduled command, no email
is delivered. A link expires 15 minutes after the worker generates it. Queued
requests older than one hour are not sent. Failed SMTP deliveries are retried on
the next command run, up to three attempts. Interrupted sends can be retried
after a lease of at least five minutes (four times the SMTP timeout if longer);
this may deliver a duplicate email, and only the latest
token for that request is accepted. Monitor the worker's error logs and queue
(`pending`, `sending`, `failed`, `sent`). Tokens, message contents and SMTP errors
are not written to application logs. The worker deletes request history older
than seven days and expired rate counters older than two hours.

### Frontend contract

The existing mobile phone/INN/email-code format on `/api/auth/password-reset/`
is preserved when `phone`/`username` is provided without `email`. It still uses
the existing `views.password_reset` implementation and its 15-minute challenge;
the new dispatcher adds an IP rate limit. The email-only format below does not
require a phone, INN or new password when requesting a link. SMTP settings and
`PASSWORD_RESET_TIMEOUT` are shared. Recovery password validators apply only
to the new confirmation endpoint and do not alter registration/admin policies.

Request a link (no authentication):

```http
POST /api/auth/password-reset/
Content-Type: application/json

{"email":"client@example.com"}
```

Valid requests return **202** and the same `detail` message regardless of whether
an eligible account exists. No token is returned in the API. Up to three emails
per normalized address per hour; excess requests still return the same 202.
There is also a limit of 20 requests per source IP per hour (429, Retry-After).
Counts use fixed UTC hour windows and a shared database, not per-process memory.
Requests perform no synchronous SMTP operation. They do not promise identical
response times at the database level.

The email contains `PASSWORD_RESET_URL?token=<64-character-random-token>`.
Opening the link must only show a form; **GET never changes the password**.
The frontend should remove the token from the address bar after reading it,
set `Referrer-Policy: no-referrer`, and avoid third-party scripts/analytics on
this page. Never log query strings containing tokens in the web server or proxy.
Submit the new password using JSON:

```http
POST /api/auth/password-reset/confirm/
Content-Type: application/json

{"token":"<token-from-email>","new_password":"<new-password>"}
```

Success returns **200** with `detail`. The user then signs in normally. All
previous API `AuthToken` values and outstanding reset requests are revoked.
Django sessions become invalid through the password change. Links are also
invalidated by an email/password change or user deactivation. Only token hashes
are stored. Weak, common, numeric-only, similar-to-user, unchanged, or over-128-
character passwords return **400** with `detail` and `errors`; they do not consume
the link. Other invalid/expired/used links return a generic **400**. Confirmation
is limited to 60 attempts per IP per hour. Both endpoints require POST and accept
JSON objects. Swagger/OpenAPI documentation is available at `/api/docs/`.

The API uses `REMOTE_ADDR` for source-IP limits and ignores user-supplied
`X-Forwarded-For`. Configure the trusted reverse proxy/application server to
populate the real client IP safely; otherwise users share the proxy's limit.

### Verification without sending real mail

```sh
.venv/bin/python manage.py test api.test_email_password_reset
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
```

Tests use an in-memory mail backend. As with registration notifications, console,
file and dummy mail backends do not count as delivered mail and leave jobs failed
for retry. Use a local SMTP capture server for manual development.
