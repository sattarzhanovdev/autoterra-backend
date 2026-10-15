import io
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse
from openpyxl import Workbook

from .models import KnowledgeChunk, KnowledgeDocument, KnowledgeDriveFile
from .services.google_drive_knowledge import DriveClient, DriveError, sync_folder
from .services.knowledge_documents import MIME_TYPES, process_document, save_upload, document_path
from .services.knowledge_retrieval import retrieve
from .test_knowledge_documents import pdf_bytes, docx_bytes


class GoogleDriveKnowledgeTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        config = override_settings(PRIVATE_KNOWLEDGE_ROOT=Path(self.tmp.name), HF_API_TOKEN='',
            GOOGLE_DRIVE_KNOWLEDGE_FOLDER_ID='folder123')
        config.enable()
        self.addCleanup(config.disable)
        self.parser = patch('api.services.google_drive_knowledge.run_parser', side_effect=process_document)
        self.parser.start()
        self.addCleanup(self.parser.stop)
        self.drive = MagicMock()
        self.items = []
        self.data = {}
        self.drive.list_files.side_effect = lambda folder: list(self.items)
        self.drive.download.side_effect = self.download

    def download(self, item, extension, stream):
        data = self.data[item['id']]
        stream.write(data)
        stream.seek(0)
        return len(data)

    def item(self, file_id='file1', name='primer.txt', mime='text/plain', data=b'Primer drying temperature is 20 degrees.'):
        item = {'id': file_id, 'name': name, 'mimeType': mime, 'modifiedTime': '2026-10-15T00:00:00Z',
                'version': '1', 'size': str(len(data))}
        self.items.append(item)
        self.data[file_id] = data
        return item

    def sync(self, **kwargs):
        return sync_folder(self.drive, 'folder123', **kwargs)

    def test_new_file_is_private_and_retrievable(self):
        self.item()
        stats = self.sync()
        self.assertEqual(stats['created'], 1)
        source = KnowledgeDriveFile.objects.get()
        self.assertEqual(source.status, 'ready')
        self.assertEqual(source.document.source_type, 'google_drive')
        self.assertTrue(document_path(source.document).exists())
        result = retrieve('Primer drying')[0]
        self.assertEqual(result['source']['url'], source.drive_url)
        self.assertEqual(result['source']['filename'], 'primer.txt')
        self.assertEqual(source.version, '1')
        self.assertIsNotNone(source.last_synced_at)

    def test_unchanged_sync_is_read_only_and_does_not_download(self):
        self.item()
        self.sync()
        before = list(KnowledgeDriveFile.objects.values())
        doc = list(KnowledgeDocument.objects.values())
        chunks = list(KnowledgeChunk.objects.values())
        self.drive.download.reset_mock()
        stats = self.sync()
        self.assertEqual(stats['unchanged'], 1)
        self.assertEqual(list(KnowledgeDriveFile.objects.values()), before)
        self.assertEqual(list(KnowledgeDocument.objects.values()), doc)
        self.assertEqual(list(KnowledgeChunk.objects.values()), chunks)
        self.drive.download.assert_not_called()

    def test_changed_file_replaces_document_without_duplicates(self):
        item = self.item()
        self.sync()
        old_id = KnowledgeDriveFile.objects.get().document_id
        item['version'] = '2'
        self.data['file1'] = b'Zebraprimer cures in 48 hours.'
        stats = self.sync()
        source = KnowledgeDriveFile.objects.get()
        self.assertEqual(stats['updated'], 1)
        self.assertNotEqual(source.document_id, old_id)
        self.assertEqual(KnowledgeDocument.objects.count(), 1)
        self.assertEqual(KnowledgeDriveFile.objects.count(), 1)
        self.assertTrue(retrieve('Zebraprimer'))
        self.assertFalse(KnowledgeChunk.objects.filter(text__contains='20 degrees').exists())

    def test_removed_file_deactivated_and_reappearance_reprocessed(self):
        self.item()
        self.sync()
        self.items.clear()
        self.assertEqual(self.sync()['deactivated'], 1)
        source = KnowledgeDriveFile.objects.get()
        self.assertFalse(source.active)
        self.assertFalse(source.document.active)
        self.assertEqual(retrieve('Primer'), [])
        self.assertEqual(self.sync()['deactivated'], 0)
        self.item()
        self.sync()
        self.assertTrue(retrieve('Primer'))
        self.assertEqual(KnowledgeDriveFile.objects.count(), 1)

    def test_partial_inventory_error_never_deactivates_files(self):
        self.item()
        self.sync()
        self.drive.list_files.side_effect = DriveError('incomplete_listing')
        with self.assertRaises(DriveError):
            self.sync()
        self.assertTrue(KnowledgeDriveFile.objects.get().active)
        self.assertTrue(retrieve('Primer'))

    def test_broken_pdf_does_not_block_good_file(self):
        self.item('broken', 'broken.pdf', 'application/pdf', b'%PDF-broken-content')
        self.item('good')
        stats = self.sync()
        self.assertEqual(stats['failed'], 1)
        self.assertEqual(stats['created'], 1)
        self.assertEqual(KnowledgeDriveFile.objects.get(file_id='broken').status, 'error')
        self.assertTrue(retrieve('Primer'))

    def test_changed_broken_document_excludes_old_content_and_is_retryable(self):
        item = self.item()
        self.sync()
        item['version'] = '2'
        self.data['file1'] = b'\x00binary'
        self.assertEqual(self.sync()['failed'], 1)
        self.assertEqual(retrieve('Primer'), [])
        self.assertEqual(self.sync()['unchanged'], 1)
        self.data['file1'] = b'Primer repaired.'
        self.assertEqual(self.sync(retry_failed=True)['updated'], 1)
        self.assertTrue(retrieve('Primer'))

    def test_interrupted_staging_document_is_cleaned_up_on_retry(self):
        self.item()
        self.sync()
        source = KnowledgeDriveFile.objects.get()
        staging = save_upload(SimpleUploadedFile('unfinished.txt', b'Old staging', 'text/plain'), None)
        staging.active = False
        staging.save()
        source.pending_document, source.status = staging, 'pending'
        source.save()
        self.assertEqual(self.sync()['updated'], 1)
        source.refresh_from_db()
        self.assertIsNone(source.pending_document_id)
        self.assertFalse(KnowledgeDocument.objects.filter(pk=staging.pk).exists())
        self.assertEqual(KnowledgeDocument.objects.count(), 1)

    def test_unsupported_video_and_other_files_recorded(self):
        self.item('video', 'training.mp4', 'video/mp4', b'video')
        self.item('zip', 'archive.zip', 'application/zip', b'zip')
        self.assertEqual(self.sync()['failed'], 2)
        self.assertEqual(KnowledgeDriveFile.objects.get(file_id='video').error, 'transcription_required')
        self.assertEqual(KnowledgeDriveFile.objects.get(file_id='zip').status, 'unsupported')
        self.drive.download.assert_not_called()
        self.assertEqual(self.sync()['unchanged'], 2)

    def test_pdf_preserves_page(self):
        self.item('pdf', 'manual.pdf', 'application/pdf', pdf_bytes())
        self.sync()
        self.assertEqual(retrieve('Primer')[0]['source']['page'], 1)

    def test_docx_and_native_google_docs_export(self):
        self.item('docx', 'manual.docx', MIME_TYPES['docx'], docx_bytes('Primer instructions'))
        self.item('native', 'Google instructions', 'application/vnd.google-apps.document', docx_bytes('Zebraprimer instruction'))
        stats = self.sync()
        self.assertEqual(stats['created'], 2)
        self.assertTrue(retrieve('Zebraprimer'))
        self.assertEqual(KnowledgeDriveFile.objects.get(file_id='native').document.file_type, 'docx')

    def test_xlsx_preserves_sheet_and_row_range_and_does_not_execute_formulas(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = 'Primer table'
        sheet.append(['Material', 'Temperature'])
        sheet.append(['Zebraprimer', 20])
        sheet.append(['Formula', '=1+1'])
        stream = io.BytesIO()
        workbook.save(stream)
        self.item('xlsx', 'materials.xlsx', MIME_TYPES['xlsx'], stream.getvalue())
        self.assertEqual(self.sync()['created'], 1)
        result = retrieve('Zebraprimer')[0]
        self.assertEqual(result['source']['sheet'], 'Primer table')
        self.assertEqual(result['source']['rowStart'], 1)
        self.assertEqual(result['source']['rowEnd'], 3)
        self.assertNotIn('=1+1', result['text'])

    def test_html_ignores_scripts_and_styles_and_does_not_fetch_resources(self):
        self.item('html', 'manual.html', MIME_TYPES['html'], b'<h1>Zebraprimer</h1><script>BADSECRET fetch("https://bad")</script><style>HIDDEN</style><p>Dry 24 hours</p>')
        self.assertEqual(self.sync()['created'], 1)
        text = retrieve('Zebraprimer')[0]['text']
        self.assertIn('Dry 24 hours', text)
        self.assertNotIn('BADSECRET', text)
        self.assertNotIn('HIDDEN', text)

    @override_settings(KNOWLEDGE_MAX_FILE_SIZE=5)
    def test_file_size_limit_is_applied_to_imports(self):
        self.item()
        self.assertEqual(self.sync()['failed'], 1)
        self.assertEqual(KnowledgeDriveFile.objects.get().error, 'too_large')
        self.assertFalse(KnowledgeDocument.objects.exists())

    def test_download_error_is_redacted(self):
        self.item()
        self.drive.download.side_effect = RuntimeError('SECRET_CREDENTIALS')
        self.assertEqual(self.sync()['failed'], 1)
        self.assertEqual(KnowledgeDriveFile.objects.get().error, 'sync_file_failed')

    def test_management_command_outputs_stats_with_mocked_client(self):
        self.item()
        output = io.StringIO()
        with patch('api.management.commands.sync_google_drive_knowledge.DriveClient', return_value=self.drive):
            call_command('sync_google_drive_knowledge', stdout=output)
        self.assertEqual(json.loads(output.getvalue())['created'], 1)
        self.drive.close.assert_called_once()

    def test_management_command_reports_failures_without_credentials(self):
        with patch('api.management.commands.sync_google_drive_knowledge.DriveClient', side_effect=RuntimeError('SECRET')):
            with self.assertRaisesMessage(CommandError, 'drive_sync_failed'):
                call_command('sync_google_drive_knowledge')

    def test_drive_admin_status_and_permissions(self):
        self.item()
        self.sync()
        staff = User.objects.create_superuser('drive-admin', 'staff@example.com', 'pw')
        self.client.force_login(staff)
        response = self.client.get(reverse('admin:api_knowledgedrivefile_changelist'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'primer.txt')
        self.assertContains(response, 'Открыть оригинал')
        self.assertEqual(self.client.get('/api/knowledge-documents/1/download/').status_code, 401)


@override_settings(GOOGLE_DRIVE_REQUEST_TIMEOUT=10, GOOGLE_DRIVE_DOWNLOAD_TIMEOUT=30)
class DriveHTTPTests(TestCase):
    """Transport is mocked too: no test can call Google or read actual credentials."""
    def make_client(self):
        client = DriveClient.__new__(DriveClient)
        client.session = MagicMock()
        return client

    def response(self, data):
        response = MagicMock(status_code=200)
        response.__enter__.return_value = response
        response.json.return_value = data
        return response

    def test_service_account_uses_readonly_scope_and_bounded_refresh(self):
        with override_settings(GOOGLE_DRIVE_CREDENTIALS_FILE='/server/secret.json'), patch(
            'google.oauth2.service_account.Credentials.from_service_account_file') as credentials, patch(
            'google.auth.transport.requests.AuthorizedSession') as session:
            client = DriveClient()
        credentials.assert_called_once_with('/server/secret.json', scopes=['https://www.googleapis.com/auth/drive.readonly'])
        session.assert_called_once_with(credentials.return_value, refresh_timeout=10)
        client.close()

    def test_credentials_errors_do_not_leak_path_or_secret(self):
        with override_settings(GOOGLE_DRIVE_CREDENTIALS_FILE='/secret'), patch(
            'google.oauth2.service_account.Credentials.from_service_account_file', side_effect=ValueError('SECRET')):
            with self.assertRaisesMessage(DriveError, 'invalid_credentials_file'):
                DriveClient()

    def test_listing_is_paginated_and_recursive(self):
        client = self.make_client()
        client.session.get.side_effect = [self.response({'nextPageToken': 'next', 'files': [
            {'id': 'sub', 'name': 'nested', 'mimeType': 'application/vnd.google-apps.folder'}]}),
            self.response({'files': [{'id': 'rootfile', 'mimeType': 'text/plain', 'name': 'a'}]}),
            self.response({'files': [{'id': 'childfile', 'mimeType': 'text/plain', 'name': 'b'}]})]
        files = client.list_files('root')
        self.assertEqual({item['id'] for item in files}, {'rootfile', 'childfile'})
        self.assertEqual(client.session.get.call_count, 3)
        self.assertEqual(client.session.get.call_args.kwargs['timeout'], 10)

    def test_incomplete_listing_aborts(self):
        client = self.make_client()
        client.session.get.return_value = self.response({'incompleteSearch': True})
        with self.assertRaisesMessage(DriveError, 'incomplete_listing'):
            client.list_files('root')

    def test_native_export_and_blob_download(self):
        client = self.make_client()
        response = self.response({})
        response.headers = {}
        response.iter_content.return_value = [b'hello']
        client.session.get.return_value = response
        client.download({'id': 'doc', 'mimeType': 'application/vnd.google-apps.document'}, 'docx', io.BytesIO())
        self.assertTrue(client.session.get.call_args.args[0].endswith('/doc/export'))
        self.assertEqual(client.session.get.call_args.kwargs['params']['mimeType'], MIME_TYPES['docx'])
        client.download({'id': 'txt', 'mimeType': 'text/plain'}, 'txt', io.BytesIO())
        self.assertEqual(client.session.get.call_args.kwargs['params']['alt'], 'media')

    @override_settings(KNOWLEDGE_MAX_FILE_SIZE=5)
    def test_stream_limit_catches_missing_or_false_content_length(self):
        client = self.make_client()
        response = self.response({})
        response.headers = {}
        response.iter_content.return_value = [b'123', b'456']
        client.session.get.return_value = response
        with self.assertRaisesMessage(DriveError, 'file_too_large'):
            client.download({'id': 'txt', 'mimeType': 'text/plain'}, 'txt', io.BytesIO())

    def test_transport_and_http_errors_are_redacted(self):
        client = self.make_client()
        client.session.get.side_effect = RuntimeError('SECRET')
        with self.assertRaisesMessage(DriveError, 'drive_request_failed'):
            client.list_files('root')
        client.session.get.side_effect = None
        client.session.get.return_value = MagicMock(status_code=403)
        with self.assertRaisesMessage(DriveError, 'drive_http_403'):
            client.list_files('root')
