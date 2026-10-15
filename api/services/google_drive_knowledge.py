"""Read-only Drive mirror. All network access stays outside the chat request."""
import hashlib
import json
import logging
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from django.conf import settings
from django.core.files.uploadedfile import UploadedFile
from django.db import connection, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from api.models import KnowledgeDocument, KnowledgeDriveFile
from api.management.commands.process_knowledge_documents import run_parser
from .knowledge_documents import DocumentError, MIME_TYPES, private_root, save_upload

logger = logging.getLogger(__name__)
FOLDER_MIME = 'application/vnd.google-apps.folder'
EXPORTS = {
    'application/vnd.google-apps.document': 'docx',
    'application/vnd.google-apps.spreadsheet': 'xlsx',
}
EXTENSIONS = {mime: ext for ext, mime in MIME_TYPES.items()}


class DriveError(ValueError):
    """Only controlled codes may leave this integration; never Google error bodies."""


class APIKeyLogFilter(logging.Filter):
    """urllib3 DEBUG/retry logging otherwise includes the complete query string."""
    def filter(self, record):
        record.msg = re.sub(r'([?&]key=)[^&\s\'\"]+', r'\1[REDACTED]', record.getMessage())
        record.args = ()
        return True


API_KEY_LOG_FILTER = APIKeyLogFilter()


class DriveClient:
    def __init__(self):
        path = settings.GOOGLE_DRIVE_CREDENTIALS_FILE
        self.public_mode = not bool(path)
        self.api_key = '' if path else settings.GOOGLE_DRIVE_API_KEY
        if self.public_mode:
            if not self.api_key:
                raise DriveError('credentials_not_configured')
            import requests
            self.session = requests.Session()
            for name in ('urllib3.connectionpool', 'urllib3.util.retry'):
                logging.getLogger(name).addFilter(API_KEY_LOG_FILTER)
            return
        from google.oauth2.service_account import Credentials
        from google.auth.transport.requests import AuthorizedSession

        try:
            credentials = Credentials.from_service_account_file(path, scopes=[
                'https://www.googleapis.com/auth/drive.readonly'])
            self.session = AuthorizedSession(credentials, refresh_timeout=settings.GOOGLE_DRIVE_REQUEST_TIMEOUT)
        except Exception:
            raise DriveError('invalid_credentials_file') from None

    def close(self):
        self.session.close()

    def get(self, path, **kwargs):
        try:
            params = dict(kwargs.pop('params', {}))
            if self.public_mode:
                params['key'] = self.api_key
            else:
                kwargs['max_allowed_time'] = settings.GOOGLE_DRIVE_REQUEST_TIMEOUT
            response = self.session.get('https://www.googleapis.com/drive/v3/' + path,
                timeout=settings.GOOGLE_DRIVE_REQUEST_TIMEOUT,
                params=params, **kwargs)
            if response.status_code != 200:
                code = response.status_code
                response.close()
                raise DriveError(f'drive_http_{code}')
            return response
        except DriveError:
            raise
        except Exception:
            raise DriveError('drive_request_failed') from None

    def list_files(self, folder_id):
        """Complete recursive inventory before any deactivation; reject partial results."""
        folders, visited, files = [folder_id], set(), {}
        while folders:
            parent = folders.pop()
            if parent in visited:
                continue
            visited.add(parent)
            page_token = None
            page_tokens = set()
            while True:
                params = {'q': f"'{parent}' in parents and trashed = false", 'pageSize': 1000,
                    'supportsAllDrives': 'true', 'includeItemsFromAllDrives': 'true',
                    'fields': 'nextPageToken,incompleteSearch,files(id,name,mimeType,modifiedTime,md5Checksum,version,size)'}
                if page_token:
                    params['pageToken'] = page_token
                with self.get('files', params=params) as response:
                    data = response.json()
                if data.get('incompleteSearch'):
                    raise DriveError('incomplete_listing')
                for item in data.get('files', []):
                    if not re.fullmatch(r'[A-Za-z0-9_-]+', item['id']):
                        raise DriveError('invalid_file_id')
                    if item['mimeType'] == FOLDER_MIME:
                        folders.append(item['id'])
                    else:
                        files[item['id']] = item
                    if len(files) + len(visited) + len(folders) > settings.GOOGLE_DRIVE_MAX_FILES:
                        raise DriveError('inventory_limit')
                page_token = data.get('nextPageToken')
                if not page_token:
                    break
                if page_token in page_tokens:
                    raise DriveError('repeated_page_token')
                page_tokens.add(page_token)
        return list(files.values())

    def download(self, item, extension, out):
        limit = settings.KNOWLEDGE_MAX_FILE_SIZE
        if int(item.get('size', 0)) > limit:
            raise DriveError('file_too_large')
        native = item['mimeType'] in EXPORTS
        if native and self.public_mode:
            raise DriveError('workspace_export_requires_oauth')
        params = {'mimeType': MIME_TYPES[extension]} if native else {'alt': 'media', 'supportsAllDrives': 'true'}
        endpoint = 'files/' + quote(item['id'], safe='') + ('/export' if native else '')
        started, size = time.monotonic(), 0
        with self.get(endpoint, params=params, stream=True) as response:
            if int(response.headers.get('Content-Length', 0)) > limit:
                raise DriveError('file_too_large')
            for block in response.iter_content(64 * 1024):
                size += len(block)
                if size > limit:
                    raise DriveError('file_too_large')
                if time.monotonic() - started > settings.GOOGLE_DRIVE_DOWNLOAD_TIMEOUT:
                    raise DriveError('download_timeout')
                out.write(block)
        out.seek(0)
        return size


@contextmanager
def sync_lock():
    """Cross-process/host PostgreSQL lock; local SQLite uses the shared private disk."""
    if connection.vendor == 'postgresql':
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_try_advisory_lock(%s)', [1840271501])
            acquired = cursor.fetchone()[0]
        if not acquired:
            raise DriveError('sync_already_running')
        try:
            yield
        finally:
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_unlock(%s)', [1840271501])
    else:
        import fcntl
        with (private_root() / '.drive-sync.lock').open('a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DriveError('sync_already_running') from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def deactivate(source):
    if source.document_id:
        KnowledgeDocument.objects.filter(pk=source.document_id).update(active=False)


def sync_file(client, item, folder_id, retry_failed=False):
    fingerprint = hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
    source, created = KnowledgeDriveFile.objects.get_or_create(file_id=item['id'], defaults={
        'folder_id': folder_id, 'filename': item['name'][:255], 'mime_type': item['mimeType'],
        'drive_url': f"https://drive.google.com/file/d/{quote(item['id'], safe='')}/view"})
    document = source.document
    healthy = (source.status != 'ready' or (document and document.active and document.status == 'ready'))
    if source.folder_id == folder_id and source.fingerprint == fingerprint and source.active and healthy and source.status != 'pending' and not (
        retry_failed and source.status in ('error', 'unsupported')):
        return 'unchanged'
    deactivate(source)
    source.folder_id = folder_id
    source.filename = item['name'][:255]
    source.mime_type = item['mimeType']
    source.modified_time = parse_datetime(item.get('modifiedTime', ''))
    source.version = str(item.get('version', ''))[:128]
    source.checksum = item.get('md5Checksum', '')[:128]
    source.fingerprint = fingerprint
    source.last_synced_at = timezone.now()
    source.active = True
    source.status, source.error = 'pending', ''
    source.save()
    if source.pending_document_id:
        # A previous sync may have been killed while its parser was running.
        # The inactive staging document is tracked so it cannot become an orphan.
        source.pending_document.delete()
        source.pending_document = None
    extension = EXPORTS.get(source.mime_type) or EXTENSIONS.get(source.mime_type)
    if extension is None:
        source.status = 'unsupported'
        source.error = 'transcription_required' if source.mime_type.startswith(('video/', 'audio/')) else 'unsupported_format'
        source.save(update_fields=['status', 'error'])
        return 'failed'
    if source.mime_type in EXPORTS and client.public_mode:
        source.status, source.error = 'unsupported', 'workspace_export_requires_oauth'
        source.save(update_fields=['status', 'error'])
        return 'failed'
    new_doc = None
    try:
        # Private OS temporary file is always closed and unlinked, including errors.
        with tempfile.TemporaryFile() as stream:
            size = client.download(item, extension, stream)
            name = Path(source.filename).stem + '.' + extension
            upload = UploadedFile(stream, name=name, content_type=MIME_TYPES[extension], size=size)
            # The normal worker must not claim this document before source metadata is set.
            with transaction.atomic():
                new_doc = save_upload(upload, None, source.filename)
                token = uuid.uuid4().hex
                new_doc.active = False
                new_doc.source_type = 'google_drive'
                new_doc.source_url = source.drive_url
                new_doc.processing_token = token
                new_doc.claimed_at = timezone.now()
                new_doc.save()
                source.pending_document = new_doc
                source.save(update_fields=['pending_document'])
        run_parser(new_doc.pk, token)
        new_doc.refresh_from_db()
        if new_doc.status != 'ready':
            raise DriveError(new_doc.error_code or 'processing_failed')
        with transaction.atomic():
            old_id = source.document_id
            new_doc.active = True
            new_doc.save(update_fields=['active'])
            source.document = new_doc
            source.pending_document = None
            source.status, source.error = 'ready', ''
            source.save(update_fields=['document', 'pending_document', 'status', 'error'])
            if old_id:
                KnowledgeDocument.objects.filter(pk=old_id).delete()
        return 'created' if created else 'updated'
    except Exception as exc:
        # Only our controlled validation codes; never exception text/tracebacks/tokens.
        code = exc.code if isinstance(exc, DocumentError) else str(exc) if isinstance(exc, DriveError) else 'sync_file_failed'
        if new_doc:
            new_doc.delete()
            # SET_NULL updates the DB, but Django still caches the deleted object
            # on source.pending_document. Clear it before persisting the error.
            source.refresh_from_db()
        source.status, source.error = 'error', code[:255]
        source.save(update_fields=['status', 'error'])
        logger.warning('Drive sync file failed; file_id=%s code=%s', source.file_id, source.error)
        return 'failed'


def sync_folder(client, folder_id, retry_failed=False):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', folder_id):
        raise DriveError('invalid_folder_id')
    with sync_lock():
        # Inventory errors abort without interpreting a partial listing as deletion.
        files = client.list_files(folder_id)
        stats = dict(found=len(files), created=0, updated=0, unchanged=0, deactivated=0, failed=0)
        seen = set()
        for item in files:
            seen.add(item['id'])
            try:
                outcome = sync_file(client, item, folder_id, retry_failed)
            except Exception:
                logger.warning('Drive sync unexpected file error; file_id=%s', item['id'])
                outcome = 'failed'
            stats[outcome] += 1
        for source in KnowledgeDriveFile.objects.filter(folder_id=folder_id, active=True).exclude(file_id__in=seen):
            with transaction.atomic():
                deactivate(source)
                source.active, source.status = False, 'inactive'
                source.last_synced_at = timezone.now()
                source.save(update_fields=['active', 'status', 'last_synced_at'])
            stats['deactivated'] += 1
        return stats
