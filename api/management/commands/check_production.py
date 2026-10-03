import json
from pathlib import Path
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from api.models import Distributor, Region


class Command(BaseCommand):
    help = 'Validate production configuration without printing secret values.'

    def handle(self, *args, **options):
        errors = []

        def check(ok, label):
            self.stdout.write(f'[{"OK" if ok else "ERROR"}] {label}')
            if not ok:
                errors.append(label)

        check(not settings.DEBUG, 'DEBUG disabled')
        try:
            key = settings.SECRET_KEY
        except ImproperlyConfigured:
            key = ""
        check(len(key) >= 50 and len(set(key)) >= 5 and not key.startswith(('django-insecure-', 'dev-')), 'Strong SECRET_KEY configured')
        check(bool(settings.ALLOWED_HOSTS) and '*' not in settings.ALLOWED_HOSTS, 'Explicit ALLOWED_HOSTS configured')
        check(not settings.CORS_ALLOW_ALL_ORIGINS, 'CORS wildcard disabled')
        check(settings.DATABASES['default']['ENGINE'] == 'django.db.backends.postgresql', 'PostgreSQL configured')
        check(bool(settings.EMAIL_HOST_USER), 'SMTP user configured')
        check(bool(settings.EMAIL_HOST_PASSWORD), 'SMTP password configured')
        check(settings.EMAIL_BACKEND == 'django.core.mail.backends.smtp.EmailBackend', 'SMTP backend enabled')
        check(bool(settings.REGISTRATION_NOTIFICATION_EMAILS), 'Registration recipients configured')
        check(bool(settings.ORDER_NOTIFICATION_EMAILS), 'Order recipients configured')
        check(bool(settings.YOOKASSA_SHOP_ID and settings.YOOKASSA_SECRET_KEY), 'YooKassa configured')
        check(settings.YOOKASSA_RETURN_URL.startswith('https://'), 'YooKassa HTTPS return URL configured')
        check(settings.ORDER_RESERVATION_TTL_HOURS > 0, 'Reservation TTL positive')
        try:
            account = json.loads(Path(settings.FCM_SERVICE_ACCOUNT_FILE).read_text())
            valid = account.get('type') == 'service_account' and all(account.get(k) for k in ('project_id', 'private_key', 'client_email'))
        except (OSError, ValueError, TypeError):
            valid = False
        check(bool(valid), 'Firebase service account configured')
        try:
            executor = MigrationExecutor(connection)
            check(not executor.migration_plan(executor.loader.graph.leaf_nodes()), 'Migrations applied')
            check(Region.objects.filter(is_active=True).exists(), 'Active regions exist')
            check(Distributor.objects.filter(is_active=True).exists(), 'Active distributors exist')
            check(not Region.objects.filter(is_active=True).exclude(distributor__is_active=True).exists(), 'Active regions have active distributors')
            check(not Distributor.objects.filter(is_active=True).exclude(user__is_active=True, user__profile__role='distributor').exists(), 'Active distributors have active distributor accounts')
            for region in Region.objects.filter(is_active=True, manager__isnull=True):
                self.stdout.write(f'[WARNING] Region "{region.name}" has no manager')
        except Exception:
            check(False, 'Database accessible and schema readable')
        if errors:
            raise CommandError(f'Production checks failed: {len(errors)}')
