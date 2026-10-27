import io
import json
import os
import runpy
import tempfile
import zipfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from reportlab.pdfgen import canvas

from .models import AuthToken, KnowledgeDocument, KnowledgeChunk, KnowledgeCard
from .management.commands.process_knowledge_documents import claim_document
from .services.knowledge_documents import process_document, split_text, document_path
from .services.knowledge_retrieval import retrieve, candidates

DOCX_MIME = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'


def docx_bytes(text='Растворитель наносится на поверхность перед грунтованием.'):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('[Content_Types].xml', '<Types><Override ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr('word/document.xml', f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>')
    return stream.getvalue()


def pdf_bytes(text='Primer drying temperature is 20 degrees.'):
    stream = io.BytesIO()
    pdf = canvas.Canvas(stream)
    if text:
        pdf.drawString(40, 700, text)
    pdf.showPage()
    pdf.save()
    return stream.getvalue()


@override_settings(HF_API_TOKEN='')
class KnowledgeDocumentTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = override_settings(PRIVATE_KNOWLEDGE_ROOT=Path(self.tmp.name).resolve())
        self.config.enable()
        self.addCleanup(self.config.disable)
        self.expert = self.user('expert', 'ai_expert')
        self.other = self.user('other', 'ai_expert')
        self.admin = self.user('admin', 'admin')
        self.auth = self.headers(self.expert)

    def user(self, name, role):
        user = User.objects.create_user(name)
        user.profile.role = role
        user.profile.save()
        AuthToken.objects.create(user=user, key=name + '-token')
        return user

    def headers(self, user):
        return {'HTTP_AUTHORIZATION': 'Bearer ' + user.username + '-token'}

    def upload(self, name='manual.txt', content=b'Primer drying temperature is 20 degrees.', mime='text/plain', auth=None):
        return self.client.post('/api/knowledge-documents/upload/', {'file': SimpleUploadedFile(name, content, mime)}, **(auth or self.auth))

    def processed(self, **kwargs):
        response = self.upload(**kwargs)
        self.assertEqual(response.status_code, 201, response.content)
        doc = KnowledgeDocument.objects.get(pk=response.json()['document']['id'])
        job = claim_document()
        self.assertEqual(job[0], doc.pk)
        process_document(*job)
        doc.refresh_from_db()
        return doc

    def test_pdf_upload_extraction_and_page(self):
        doc = self.processed(name='manual.pdf', content=pdf_bytes(), mime='application/pdf')
        self.assertEqual(doc.status, 'ready')
        self.assertEqual(doc.chunks.first().page, 1)
        self.assertIn('20 degrees', doc.chunks.first().text)

    def test_docx_upload_extraction(self):
        doc = self.processed(name='manual.docx', content=docx_bytes(), mime=DOCX_MIME)
        self.assertEqual(doc.status, 'ready')
        self.assertIn('Растворитель', doc.chunks.first().text)

    def test_invalid_extensions_mime_and_signatures(self):
        for name, content, mime in [('virus.exe', b'MZ123', 'text/plain'), ('fake.pdf', b'not a pdf', 'application/pdf'),
            ('fake.docx', b'not a zip', DOCX_MIME), ('x.txt', b'text', 'application/pdf'), ('x.txt', b'\x00\x01', 'text/plain')]:
            with self.subTest(name=name, mime=mime):
                self.assertEqual(self.upload(name, content, mime).status_code, 400)
        self.assertEqual(KnowledgeDocument.objects.count(), 0)

    @override_settings(KNOWLEDGE_MAX_FILE_SIZE=10)
    def test_too_large(self):
        self.assertEqual(self.upload(content=b'x' * 11).json()['code'], 'too_large')
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_empty_txt_is_failed(self):
        doc = self.processed(content=b' \n ')
        self.assertEqual(doc.status, 'failed')
        self.assertEqual(doc.error_code, 'empty_document')
        self.assertFalse(doc.chunks.exists())

    def test_scanned_pdf_requires_ocr(self):
        doc = self.processed(name='scan.pdf', content=pdf_bytes(''), mime='application/pdf')
        self.assertEqual(doc.status, 'failed')
        self.assertEqual(doc.error_code, 'ocr_required')

    def test_empty_docx_failed(self):
        doc = self.processed(name='empty.docx', content=docx_bytes(''), mime=DOCX_MIME)
        self.assertEqual(doc.error_code, 'empty_document')

    def test_auth_roles_and_ownership(self):
        self.assertEqual(self.client.get('/api/knowledge-documents/').status_code, 401)
        doc = self.processed()
        for role in ('client', 'manager', 'distributor', 'courier'):
            auth = self.headers(self.user(role, role))
            for url, method in [('/api/knowledge-documents/', 'get'), ('/api/knowledge-documents/upload/', 'post'),
                (f'/api/knowledge-documents/{doc.pk}/', 'delete'), (f'/api/knowledge-documents/{doc.pk}/download/', 'get'),
                (f'/api/knowledge-documents/{doc.pk}/reprocess/', 'post')]:
                self.assertEqual(getattr(self.client, method)(url, **auth).status_code, 403)
        other = self.headers(self.other)
        self.assertEqual(self.client.get('/api/knowledge-documents/', **other).json()['count'], 0)
        for suffix, method in [('', 'get'), ('', 'delete'), ('download/', 'get'), ('reprocess/', 'post')]:
            self.assertEqual(getattr(self.client, method)(f'/api/knowledge-documents/{doc.pk}/{suffix}', **other).status_code, 404)
        self.assertEqual(self.client.get('/api/knowledge-documents/', **self.headers(self.admin)).json()['count'], 1)
        self.assertEqual(self.client.get(f'/api/knowledge-documents/{doc.pk}/', **self.auth).status_code, 200)
        response = self.client.get(f'/api/knowledge-documents/{doc.pk}/download/', **self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'20 degrees', b''.join(response.streaming_content))

    def test_delete_removes_chunks_and_private_file(self):
        doc = self.processed()
        path = document_path(doc)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.delete(f'/api/knowledge-documents/{doc.pk}/', **self.headers(self.admin))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(path.exists())
        self.assertFalse(KnowledgeChunk.objects.exists())
        self.assertEqual(retrieve('Primer'), [])

    def test_reprocess_and_stale_worker_cannot_publish(self):
        doc = self.processed()
        response = self.client.post(f'/api/knowledge-documents/{doc.pk}/reprocess/', **self.auth)
        self.assertEqual(response.status_code, 202)
        doc.refresh_from_db()
        self.assertFalse(doc.chunks.exists())
        self.assertEqual(self.client.post(f'/api/knowledge-documents/{doc.pk}/reprocess/', **self.auth).status_code, 409)
        process_document(doc.pk, 'old-token')
        self.assertFalse(doc.chunks.exists())
        process_document(*claim_document())
        doc.refresh_from_db()
        self.assertEqual(doc.status, 'ready')

    def test_queue_recovers_stale_claim_and_never_double_claims(self):
        response = self.upload()
        first = claim_document()
        self.assertIsNone(claim_document())
        KnowledgeDocument.objects.filter(pk=first[0]).update(claimed_at=timezone.now() - timedelta(minutes=10))
        second = claim_document()
        self.assertEqual(first[0], second[0])
        self.assertNotEqual(first[1], second[1])
        process_document(*first)
        self.assertFalse(KnowledgeChunk.objects.exists())
        process_document(*second)
        self.assertTrue(KnowledgeChunk.objects.exists())

    def test_retrieval_combines_documents_and_only_approved_cards(self):
        doc = self.processed()
        card = KnowledgeCard.objects.create(problem='Primer drying', solution='Dry at 20 degrees.', status='approved')
        KnowledgeCard.objects.create(problem='Primer secret', solution='hidden', status='draft')
        results = retrieve('Primer drying')
        self.assertEqual({c['source']['type'] for c in results}, {'card', 'document'})
        self.assertTrue(any(c['source']['id'] == str(doc.pk) for c in results))
        self.assertFalse(any('hidden' in c['text'] for c in results))
        self.assertEqual(retrieve('unrelated zebras'), [])
        self.assertLessEqual(sum(len(c['text']) for c in results), 8500)

    def test_chat_really_sends_retrieved_context_to_hf(self):
        self.processed()
        provider = MagicMock()
        provider.__enter__.return_value.read.return_value = json.dumps({'choices': [{'message': {
            'content': json.dumps({'answer': 'Температура 20 градусов [1].', 'insufficient': False})}}]}).encode()
        with override_settings(HF_API_TOKEN='test-token', HF_CHAT_TIMEOUT=17), patch('api.services.knowledge_retrieval.urllib.request.urlopen', return_value=provider) as call:
            response = self.client.post('/api/ai/chat/', json.dumps({'message': 'Primer drying'}), content_type='application/json', **self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['sources'][0]['filename'], 'manual.txt')
        request = call.call_args.args[0]
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers['user-agent'], 'AutoTerra/1.0')
        self.assertEqual(headers['accept'], 'application/json')
        self.assertEqual(headers['content-type'], 'application/json')
        self.assertEqual(headers['authorization'], 'Bearer test-token')
        self.assertEqual(call.call_args.kwargs['timeout'], 17)
        body = json.loads(request.data)
        self.assertIn('20 degrees', body['messages'][1]['content'])
        self.assertIn('только по предоставленным', body['messages'][0]['content'])
        self.assertEqual(response.json()['answer'], 'Температура 20 градусов.')

    def test_chat_without_context_never_calls_provider(self):
        with override_settings(HF_API_TOKEN='test-token'), patch('api.services.knowledge_retrieval.urllib.request.urlopen') as call:
            response = self.client.post('/api/ai/chat/', json.dumps({'message': 'zebra'}), content_type='application/json', **self.auth)
        call.assert_not_called()
        self.assertEqual(response.json()['sources'], [])

    def test_provider_failure_returns_excerpts(self):
        self.processed()
        with override_settings(HF_API_TOKEN='test-token'), patch('api.services.knowledge_retrieval.urllib.request.urlopen', side_effect=TimeoutError):
            response = self.client.post('/api/ai/chat/', json.dumps({'message': 'Primer drying'}), content_type='application/json', **self.auth)
        self.assertIn('20 degrees', response.json()['answer'])

    def test_batch_error_does_not_block_next_file(self):
        self.assertEqual(self.upload('bad.exe', b'MZ', 'application/octet-stream').status_code, 400)
        self.assertEqual(self.upload(content=b'').status_code, 201)
        self.assertEqual(self.upload(name='good.txt').status_code, 201)
        process_document(*claim_document())
        process_document(*claim_document())
        self.assertEqual(list(KnowledgeDocument.objects.order_by('id').values_list('status', flat=True)), ['failed', 'ready'])

    def test_safe_names_and_no_public_storage_url(self):
        response = self.upload(name='../../private/manual.txt')
        doc = KnowledgeDocument.objects.get()
        self.assertEqual(doc.original_name, 'manual.txt')
        self.assertEqual(document_path(doc).parent, Path(self.tmp.name).resolve())
        self.assertNotIn('storage_name', response.json()['document'])
        self.assertNotIn('url', response.json()['document'])

    def test_overlap_and_chunk_size(self):
        text = 'primer dry ' * 1000
        chunks = list(split_text(text))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 1400 for c in chunks))
        self.assertEqual(chunks[0][-200:], chunks[1][:200])

    def test_extraction_size_limit(self):
        with override_settings(KNOWLEDGE_MAX_EXTRACTED_CHARS=5):
            doc = self.processed()
        self.assertEqual(doc.error_code, 'too_complex')


    def test_postgres_morphology_and_indexes(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Russian FTS requires PostgreSQL')
        self.processed(content='Грунтование поверхности растворителем.'.encode())
        self.assertTrue(retrieve('грунтованием поверхностей'))
        with connection.cursor() as cursor:
            cursor.execute("SELECT indexname FROM pg_indexes WHERE indexname IN ('knowledge_chunk_fts', 'knowledge_card_fts')")
            self.assertEqual({row[0] for row in cursor.fetchall()}, {'knowledge_chunk_fts', 'knowledge_card_fts'})
            cursor.execute('SET LOCAL enable_seqscan = off')
        chunk_plan = candidates(KnowledgeChunk.objects.all(), ['text'], ['грунтование']).explain()
        self.assertIn('knowledge_chunk_fts', chunk_plan)
        card_plan = candidates(KnowledgeCard.objects.filter(status='approved'),
            ['title', 'problem', 'causes', 'solution', 'restrictions'], ['грунтование']).explain()
        self.assertIn('knowledge_card_fts', card_plan)

    def test_chat_validates_message(self):
        response = self.client.post('/api/ai/chat/', json.dumps({'message': ['invalid']}), content_type='application/json', **self.auth)
        self.assertEqual(response.status_code, 400)


class KnowledgeChatSettingsTests(SimpleTestCase):
    def load_settings(self, extra_env=None):
        # Execute the actual settings module in an isolated environment; do not
        # load local .env files or any credentials during these regression tests.
        env = {'DEBUG': 'True', 'DB_ENGINE': 'sqlite3', **(extra_env or {})}
        with patch.dict(os.environ, env, clear=True), patch('pathlib.Path.exists', return_value=False):
            return runpy.run_path(str(Path(__file__).resolve().parents[1] / 'autoterra_backend' / 'settings.py'))

    def test_hf_chat_timeout_defaults_to_45_without_environment_variable(self):
        config = self.load_settings()
        self.assertEqual(config['HF_CHAT_TIMEOUT'], 45)
        self.assertIsInstance(config['HF_CHAT_TIMEOUT'], int)

    def test_hf_chat_timeout_reads_integer_environment_override(self):
        config = self.load_settings({'HF_CHAT_TIMEOUT': '90'})
        self.assertEqual(config['HF_CHAT_TIMEOUT'], 90)
        self.assertIsInstance(config['HF_CHAT_TIMEOUT'], int)
