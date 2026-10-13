from django.db import transaction
from django.http import JsonResponse, FileResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from .models import KnowledgeDocument
from .pagination import paginated_response
from .services.knowledge_documents import DocumentError, KnowledgeUploadLimit, save_upload, document_path


def authorized(request):
    from .views import _current_user, _resolve_role
    user = _current_user(request)
    if not user:
        return None, None, JsonResponse({'detail': 'Unauthorized'}, status=401)
    role = _resolve_role(user)
    if role not in ('admin', 'ai_expert'):
        return None, None, JsonResponse({'detail': 'Нет доступа'}, status=403)
    qs = KnowledgeDocument.objects.all()
    if role != 'admin':
        qs = qs.filter(uploaded_by=user)
    return user, qs, None


def format_document(doc):
    return {'id': str(doc.pk), 'title': doc.title, 'originalName': doc.original_name,
        'fileType': doc.file_type, 'size': doc.size, 'status': doc.status,
        'uploadedBy': str(doc.uploaded_by_id) if doc.uploaded_by_id else None,
        'createdAt': doc.created_at.isoformat(), 'error': doc.error, 'errorCode': doc.error_code}


@require_GET
def documents(request):
    user, qs, error = authorized(request)
    if error is not None:
        return error
    return JsonResponse(paginated_response(request, qs, format_document))


@csrf_exempt
@require_POST
def upload(request):
    user, qs, error = authorized(request)
    if error is not None:
        return error
    # One request per file gives independent outcomes for large batches.
    limiter = KnowledgeUploadLimit(request)
    request.upload_handlers.insert(0, limiter)
    files = request.FILES.getlist('file')
    if limiter.exceeded:
        return JsonResponse({'detail': 'Размер файла превышает 25 МБ.', 'code': 'too_large'}, status=400)
    if len(files) != 1 or sum(len(request.FILES.getlist(k)) for k in request.FILES) != 1:
        return JsonResponse({'detail': 'Отправьте один файл в поле file.'}, status=400)
    try:
        doc = save_upload(files[0], user, request.POST.get('title', ''))
    except DocumentError as exc:
        return JsonResponse({'detail': str(exc), 'code': exc.code}, status=400)
    return JsonResponse({'document': format_document(doc)}, status=201)


@csrf_exempt
@require_http_methods(['GET', 'DELETE'])
def detail(request, document_id):
    user, qs, error = authorized(request)
    if error is not None:
        return error
    with transaction.atomic():
        doc = qs.select_for_update().filter(pk=document_id).first()
        if not doc:
            return JsonResponse({'detail': 'Документ не найден'}, status=404)
        if request.method == 'DELETE':
            doc.delete()
            return JsonResponse({'deleted': True})
        return JsonResponse({'document': format_document(doc)})


@csrf_exempt
@require_POST
def reprocess(request, document_id):
    user, qs, error = authorized(request)
    if error is not None:
        return error
    with transaction.atomic():
        doc = qs.select_for_update().filter(pk=document_id).first()
        if not doc:
            return JsonResponse({'detail': 'Документ не найден'}, status=404)
        if doc.status == 'processing':
            return JsonResponse({'detail': 'Документ уже обрабатывается'}, status=409)
        doc.chunks.all().delete()
        doc.status, doc.error, doc.error_code = 'processing', '', ''
        doc.claimed_at, doc.processing_token = None, ''
        doc.save()
        return JsonResponse({'document': format_document(doc)}, status=202)


@require_GET
def download(request, document_id):
    user, qs, error = authorized(request)
    if error is not None:
        return error
    doc = qs.filter(pk=document_id).first()
    if not doc:
        return JsonResponse({'detail': 'Документ не найден'}, status=404)
    try:
        stream = document_path(doc).open('rb')
    except FileNotFoundError:
        return JsonResponse({'detail': 'Файл недоступен'}, status=404)
    response = FileResponse(stream, as_attachment=True, filename=doc.original_name)
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response
