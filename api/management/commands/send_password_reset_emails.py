from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from api.models import PasswordResetRateLimit, PasswordResetRequest
from api.password_reset import deliver_reset_emails


class Command(BaseCommand):
    help = "Deliver queued password reset emails; schedule every minute."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        if options["limit"] < 1:
            raise CommandError("--limit must be positive")
        try:
            sent = deliver_reset_emails(options["limit"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        PasswordResetRateLimit.objects.filter(created_at__lt=timezone.now() - timedelta(hours=2)).delete()
        PasswordResetRequest.objects.filter(created_at__lt=timezone.now() - timedelta(days=7)).delete()
        self.stdout.write(f"Sent {sent} password reset email(s).")
