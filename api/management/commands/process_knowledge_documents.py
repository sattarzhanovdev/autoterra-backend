"""Durable database queue; each parser runs in a time/memory bounded process."""
import subprocess
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from api.models import KnowledgeDocument
from api.services.knowledge_documents import process_document


def claim_document():
    stale = timezone.now() - timedelta(seconds=settings.KNOWLEDGE_PROCESS_TIMEOUT + 60)
    eligible = Q(claimed_at__isnull=True) | Q(claimed_at__lt=stale)
    ids = KnowledgeDocument.objects.filter(eligible, status='processing').order_by('id').values_list('id', flat=True)[:100]
    for document_id in list(ids):
        token = uuid.uuid4().hex
        if KnowledgeDocument.objects.filter(eligible, pk=document_id, status='processing').update(
            claimed_at=timezone.now(), processing_token=token):
            return document_id, token
    return None


def run_parser(document_id, token):
    """Shared bounded parser entrypoint for uploads and Drive sync."""
    try:
        result = subprocess.run([sys.executable, str(Path(settings.BASE_DIR) / 'manage.py'),
            'process_knowledge_documents', '--process', str(document_id), '--token', token],
            timeout=settings.KNOWLEDGE_PROCESS_TIMEOUT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        failed = result.returncode != 0
    except subprocess.TimeoutExpired:
        failed = True
    if failed:
        KnowledgeDocument.objects.filter(pk=document_id, status='processing', processing_token=token).update(
            status='failed', error_code='processing_limit', error='Превышены ограничения обработки. Проверьте файл и повторите попытку.')


class Command(BaseCommand):
    help = 'Process private knowledge documents; run continuously as a service.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')
        parser.add_argument('--process', type=int, help='Internal worker subprocess only')
        parser.add_argument('--token', default='')

    def handle(self, *args, **options):
        if options['process']:
            # Resource limits contain malformed compressed PDFs and XML archives.
            import resource
            resource.setrlimit(resource.RLIMIT_CPU, (settings.KNOWLEDGE_PROCESS_TIMEOUT, settings.KNOWLEDGE_PROCESS_TIMEOUT))
            if sys.platform.startswith('linux'):
                resource.setrlimit(resource.RLIMIT_AS, (1024 ** 3, 1024 ** 3))
            process_document(options['process'], options['token'])
            return
        while True:
            job = claim_document()
            if job:
                document_id, token = job
                run_parser(document_id, token)
                self.stdout.write(f'Processed document {document_id}')
            elif options['once']:
                break
            else:
                time.sleep(3)
