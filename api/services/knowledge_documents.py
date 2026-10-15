"""Private document ingestion. Processing runs in a bounded worker subprocess."""
import re
import uuid
import zipfile
from html.parser import HTMLParser
from pathlib import Path

from defusedxml import ElementTree
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.files.uploadhandler import FileUploadHandler, StopUpload
from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver
from pypdf import PdfReader
from openpyxl import load_workbook

from api.models import KnowledgeChunk, KnowledgeDocument

MIME_TYPES = {
    'pdf': 'application/pdf',
    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'txt': 'text/plain',
    'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'html': 'text/html',
}


class DocumentError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)



class KnowledgeUploadLimit(FileUploadHandler):
    """Stop multipart parsing before an oversized upload fills temporary disk."""
    def __init__(self, request):
        super().__init__(request)
        self.total = 0
        self.exceeded = False

    def receive_data_chunk(self, raw_data, start):
        self.total += len(raw_data)
        if self.total > settings.KNOWLEDGE_MAX_FILE_SIZE:
            self.exceeded = True
            raise StopUpload(connection_reset=True)
        return raw_data

    def file_complete(self, file_size):
        return None

def private_root():
    root = Path(settings.PRIVATE_KNOWLEDGE_ROOT).resolve()
    media = Path(settings.MEDIA_ROOT).resolve()
    if root == media or media in root.parents:
        raise ImproperlyConfigured('PRIVATE_KNOWLEDGE_ROOT must be outside MEDIA_ROOT')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def document_path(document):
    # Even corrupted database paths cannot escape the private directory.
    name = document.storage_name
    if not re.fullmatch(r'[0-9a-f]{32}\.(pdf|docx|txt|xlsx|html)', name):
        raise ValueError('Invalid storage path')
    return private_root() / name


def decode_text(data):
    if b'\x00' in data and not data.startswith((b'\xff\xfe', b'\xfe\xff')):
        raise DocumentError('invalid_text', 'TXT содержит двоичные данные.')
    for encoding in ('utf-8-sig', 'utf-16' if data.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8', 'cp1251'):
        try:
            text = data.decode(encoding)
            if any(ord(c) < 32 and c not in '\n\r\t\f' for c in text):
                break
            return text
        except UnicodeError:
            continue
    raise DocumentError('invalid_text', 'Не удалось прочитать текст TXT.')


def checked_office(stream, kind='docx'):
    try:
        archive = zipfile.ZipFile(stream)
        names = archive.namelist()
        main = 'word/document.xml' if kind == 'docx' else 'xl/workbook.xml'
        if '[Content_Types].xml' not in names or main not in names:
            raise ValueError()
        if any(n.lower().endswith(('.exe', '.dll', '.js', '.vbs', '.bin')) for n in names):
            raise ValueError()
        if sum(i.file_size for i in archive.infolist()) > 50 * 1024 * 1024 or len(names) > 5000:
            raise ValueError()
        types = archive.read('[Content_Types].xml')
        expected = (b'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'
                    if kind == 'docx' else b'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml')
        if expected not in types or b'macroEnabled' in types:
            raise ValueError()
        return archive
    except (ValueError, zipfile.BadZipFile, KeyError, RuntimeError):
        raise DocumentError(f'invalid_{kind}', f'Некорректный {kind.upper()} или небезопасное содержимое.') from None


def checked_docx(stream):
    return checked_office(stream)


def save_upload(upload, user, title=''):
    name = re.sub(r'[\x00-\x1f\x7f]', '', upload.name.replace('\\', '/').split('/')[-1])[:255]
    ext = Path(name).suffix.lower().lstrip('.')
    if ext not in MIME_TYPES or upload.content_type.split(';')[0].lower() != MIME_TYPES.get(ext):
        raise DocumentError('invalid_format', 'Разрешены PDF, DOCX, XLSX, TXT, HTML с соответствующим MIME-type.')
    if upload.size > settings.KNOWLEDGE_MAX_FILE_SIZE:
        raise DocumentError('too_large', 'Размер файла превышает 25 МБ.')
    if ext == 'pdf':
        if not upload.read(5) == b'%PDF-':
            raise DocumentError('invalid_pdf', 'Некорректный PDF.')
    elif ext in ('docx', 'xlsx'):
        checked_office(upload, ext).close()
    else:
        decode_text(upload.read())
    upload.seek(0)
    storage_name = f'{uuid.uuid4().hex}.{ext}'
    path = private_root() / storage_name
    try:
        with path.open('xb') as out:
            path.chmod(0o600)
            size = 0
            for chunk in upload.chunks():
                size += len(chunk)
                if size > settings.KNOWLEDGE_MAX_FILE_SIZE:
                    raise DocumentError('too_large', 'Размер файла превышает 25 МБ.')
                out.write(chunk)
        return KnowledgeDocument.objects.create(title=(title.strip() or name)[:255], original_name=name,
            storage_name=storage_name, file_type=ext, size=size, uploaded_by=user)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def extract_sections(document):
    with document_path(document).open('rb') as stream:
        if document.file_type == 'pdf':
            reader = PdfReader(stream)
            if reader.is_encrypted:
                raise DocumentError('encrypted_pdf', 'PDF защищён паролем.')
            if len(reader.pages) > 2000:
                raise DocumentError('too_complex', 'Слишком много страниц (максимум 2000).')
            for index, page in enumerate(reader.pages, 1):
                contents = page.get_contents()
                if contents and len(contents.get_data()) > 10 * 1024 * 1024:
                    raise DocumentError('too_complex', 'Слишком сложная страница PDF.')
                yield index, '', page.extract_text() or '', {}
        elif document.file_type == 'docx':
            with checked_docx(stream) as archive:
                root = ElementTree.fromstring(archive.read('word/document.xml'))
                ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
                section = 'Документ'
                for paragraph in root.findall('.//w:p', ns):
                    text = ''.join(t.text or '' for t in paragraph.findall('.//w:t', ns))
                    style = paragraph.find('w:pPr/w:pStyle', ns)
                    if style is not None and 'heading' in style.get('{%s}val' % ns['w'], '').lower():
                        section = text[:255]
                    yield None, section, text, {}
        elif document.file_type == 'xlsx':
            checked_office(stream, 'xlsx').close()
            stream.seek(0)
            workbook = load_workbook(stream, read_only=True, data_only=True, keep_links=False)
            try:
                for sheet in workbook.worksheets:
                    if (sheet.max_column or 0) > 256:
                        raise DocumentError('too_complex', 'В XLSX более 256 столбцов.')
                    block, first, last, header = [], None, None, ''
                    for number, row in enumerate(sheet.iter_rows(), 1):
                        if number > 100_000:
                            raise DocumentError('too_complex', 'В XLSX более 100000 строк на листе.')
                        text = ' | '.join(str(cell.value) if cell.value is not None else '' for cell in row).strip(' |')
                        if not text:
                            continue
                        if not header:
                            header = text[:400]
                        if block and sum(len(t) for t in block) + len(text) > 1100:
                            yield None, sheet.title, '\n'.join(block), {'sheet': sheet.title, 'row_start': first, 'row_end': last}
                            block, first = [f'Заголовок: {header}'], None
                        first = first or number
                        last = number
                        block.append(f'Строка {number}: {text}')
                    if block:
                        yield None, sheet.title, '\n'.join(block), {'sheet': sheet.title, 'row_start': first, 'row_end': last}
            finally:
                workbook.close()
        elif document.file_type == 'html':
            parser = TextOnlyHTML()
            parser.feed(decode_text(stream.read()))
            parser.close()
            yield None, 'HTML', '\n'.join(parser.parts), {}
        else:
            yield None, '', decode_text(stream.read()), {}


class TextOnlyHTML(HTMLParser):
    """Parse text only. No browser, network, script evaluation, or entity expansion."""
    hidden = {'script', 'style', 'noscript', 'iframe', 'object', 'template', 'svg'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.suppressed = []

    def handle_starttag(self, tag, attrs):
        if tag in self.hidden:
            self.suppressed.append(tag)

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if tag in self.suppressed:
            self.suppressed = self.suppressed[:self.suppressed.index(tag)]

    def handle_data(self, data):
        if not self.suppressed:
            self.parts.append(data)


def split_text(text, size=1400, overlap=200):
    text = re.sub(r'\s+', ' ', text).strip()
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            boundary = text.rfind(' ', start + size // 2, end)
            if boundary > start:
                end = boundary
        yield text[start:end]
        if end == len(text):
            break
        start = end - overlap


def process_document(document_id, token):
    document = KnowledgeDocument.objects.filter(pk=document_id, status='processing', processing_token=token).first()
    if not document:
        return
    try:
        chunks = []
        total = 0
        # Aggregate adjacent paragraphs within a section before splitting.
        groups = []
        for page, section, text, metadata in extract_sections(document):
            total += len(text)
            if total > settings.KNOWLEDGE_MAX_EXTRACTED_CHARS:
                raise DocumentError('too_complex', 'Документ содержит слишком много текста.')
            if groups and groups[-1][0:2] == [page, section] and groups[-1][3] == metadata:
                groups[-1][2].append(text)
            else:
                groups.append([page, section, [text], metadata])
        for page, section, texts, metadata in groups:
            for text in split_text('\n'.join(texts)):
                chunks.append(KnowledgeChunk(document_id=document_id, ordinal=len(chunks), page=page, section=section, text=text, **metadata))
        if not chunks:
            code = 'ocr_required' if document.file_type == 'pdf' else 'empty_document'
            raise DocumentError(code, 'PDF не содержит текста. Требуется OCR.' if code == 'ocr_required' else 'Документ не содержит текста.')
        with transaction.atomic():
            current = KnowledgeDocument.objects.select_for_update().filter(pk=document_id, status='processing', processing_token=token).first()
            if current:
                current.chunks.all().delete()
                KnowledgeChunk.objects.bulk_create(chunks, batch_size=100)
                current.status = 'ready'
                current.error = current.error_code = ''
                current.save(update_fields=['status', 'error', 'error_code'])
    except Exception as exc:
        # Parser exceptions may include confidential content: never return/log them.
        code = exc.code if isinstance(exc, DocumentError) else 'extraction_failed'
        error = str(exc) if isinstance(exc, DocumentError) else 'Не удалось обработать документ. Проверьте файл и повторите обработку.'
        KnowledgeDocument.objects.filter(pk=document_id, processing_token=token).update(status='failed', error=error, error_code=code)


@receiver(post_delete, sender=KnowledgeDocument)
def remove_private_file(sender, instance, **kwargs):
    transaction.on_commit(lambda: document_path(instance).unlink(missing_ok=True))
