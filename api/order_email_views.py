"""Public HTML UI authenticated exclusively by a single-use email capability."""
import logging

from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods

from api.services import order_email_actions as actions

logger = logging.getLogger(__name__)


@never_cache
@csrf_exempt
@sensitive_post_parameters('token')
@require_http_methods(['GET', 'POST'])
@sensitive_variables()
def confirm_order_from_email(request):
    context = {}
    status = 200
    try:
        # No HTTP downgrade redirects carrying the secret. Behind TLS termination,
        # configure SECURE_PROXY_SSL_HEADER only for a trusted, sanitizing proxy.
        if not request.is_secure():
            raise actions.EmailActionError('Ссылка недействительна. Откройте её через HTTPS.', 400)
        raw = request.GET.get('token', '') if request.method == 'GET' else request.POST.get('token', '')
        if request.method == 'POST':
            order = actions.confirm(raw)
            context = {'order': order, 'success': True}
        else:
            order = actions.preview(raw)
            context = {'order': order, 'token': raw,
                       'items': order.items.all()[:5], 'item_count': order.items.count()}
    except actions.EmailActionError as exc:
        context, status = {'message': exc.message}, exc.status
    except Exception:
        # Never log request, exception text or traceback containing bearer secrets.
        logger.error('Email order confirmation failed')
        context = {'message': 'Не удалось подтвердить заказ. Откройте AutoTerra или обратитесь к администратору.'}
        status = 500
    response = render(request, 'order_email_confirm.html', context, status=status)
    response['Referrer-Policy'] = 'no-referrer'
    response['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
    response['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    return response
