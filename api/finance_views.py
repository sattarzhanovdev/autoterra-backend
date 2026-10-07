"""Finance endpoints share existing bearer authentication and distributor scope."""
import csv
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile
from datetime import date, datetime, time, timedelta

from django.db import IntegrityError
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .models import Distributor
from .services.finance import build_report, export_report, import_statement
from .views import _current_user, _require_distributor_scope


def scope(request, params):
    distributor, admin, error = _require_distributor_scope(request)
    if error:
        return None, False, error
    selected = params.get('distributor_id')
    if selected:
        try:
            selected = int(selected)
        except (ValueError, TypeError):
            return None, admin, JsonResponse({'detail': 'Некорректный дистрибьютор'}, status=400)
        if distributor and selected != distributor.pk:
            return None, admin, JsonResponse({'detail': 'Нет доступа к этому дистрибьютору'}, status=403)
        distributor = Distributor.objects.filter(pk=selected).first()
        if distributor is None:
            return None, admin, JsonResponse({'detail': 'Дистрибьютор не найден'}, status=404)
    return distributor, admin, None


def period(request):
    today = timezone.localdate()
    try:
        start = date.fromisoformat(request.GET.get('date_from', today.replace(day=1).isoformat()))
        finish = date.fromisoformat(request.GET.get('date_to', today.isoformat()))
        if finish < start:
            raise ValueError()
        zone = timezone.get_current_timezone()
        return start, finish, datetime.combine(start, time.min, zone), datetime.combine(finish + timedelta(days=1), time.min, zone)
    except (ValueError, OverflowError):
        raise ValueError('Укажите период YYYY-MM-DD: начало не позже окончания') from None


@require_GET
def report(request):
    distributor, admin, error = scope(request, request.GET)
    if error:
        return error
    try:
        first, last, start, end = period(request)
        page = int(request.GET.get('page', 1))
        if page < 1:
            raise ValueError('Некорректная страница')
        result = build_report(start, end, distributor.pk if distributor else None)
    except ValueError as exc:
        return JsonResponse({'detail': str(exc)}, status=400)
    result.update(dateFrom=first.isoformat(), dateTo=last.isoformat(), distributorId=distributor.pk if distributor else None)
    # Summary and Excel always cover the entire filtered report.
    result['count'] = len(result['operations'])
    result['page'] = page
    result['pageSize'] = 100
    result['operations'] = result['operations'][(page - 1) * 100:page * 100]
    result['discrepancies'] = result['discrepancies'][:100]
    result['distributors'] = list(Distributor.objects.values('id', 'name')) if admin else []
    return JsonResponse(result)


@csrf_exempt
@require_POST
def registry_import(request):
    distributor, _, error = scope(request, request.POST)
    if error:
        return error
    file = request.FILES.get('file')
    if not file:
        return JsonResponse({'detail': 'Загрузите file: CSV или XLSX'}, status=400)
    if file.size > 10 * 1024 * 1024:
        return JsonResponse({'detail': 'Размер файла не должен превышать 10 МБ'}, status=400)
    try:
        result = import_statement(file.read(), file.name, _current_user(request), distributor)
    except ValueError as exc:
        return JsonResponse({'detail': str(exc)}, status=400)
    except (csv.Error, ParseError, BadZipFile, UnicodeError):
        return JsonResponse({'detail': 'Поврежденный файл реестра'}, status=400)
    except IntegrityError:
        return JsonResponse({'detail': 'Реестр импортируется параллельно. Повторите загрузку.'}, status=409)
    return JsonResponse(result, status=201)


@require_GET
def export(request):
    distributor, _, error = scope(request, request.GET)
    if error:
        return error
    try:
        first, last, start, end = period(request)
        content = export_report(build_report(start, end, distributor.pk if distributor else None), first, last)
    except ValueError as exc:
        return JsonResponse({'detail': str(exc)}, status=400)
    response = HttpResponse(content, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="autoterra-yookassa-{first}-{last}.xlsx"'
    return response
