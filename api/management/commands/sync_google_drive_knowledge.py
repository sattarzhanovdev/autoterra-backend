import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from api.services.google_drive_knowledge import DriveClient, DriveError, sync_folder


class Command(BaseCommand):
    help = 'Mirror the configured read-only Google Drive knowledge folder (safe for cron/systemd).'

    def add_arguments(self, parser):
        parser.add_argument('--retry-failed', action='store_true', help='Retry unchanged failed/unsupported files.')

    def handle(self, *args, **options):
        client = None
        try:
            if not settings.GOOGLE_DRIVE_KNOWLEDGE_FOLDER_ID:
                raise DriveError('folder_not_configured')
            client = DriveClient()
            stats = sync_folder(client, settings.GOOGLE_DRIVE_KNOWLEDGE_FOLDER_ID, options['retry_failed'])
        except Exception as exc:
            code = str(exc) if isinstance(exc, DriveError) else 'drive_sync_failed'
            raise CommandError(code) from None
        finally:
            if client:
                client.close()
        self.stdout.write(json.dumps(stats))
        if stats['failed']:
            raise CommandError('Some files failed or are unsupported; see Google Drive knowledge admin.')
