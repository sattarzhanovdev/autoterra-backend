import json

from django.http import JsonResponse


class ApiStatelessMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith('/api/'):
            request._dont_enforce_csrf_checks = True
            if request.content_type == 'application/json' and request.body:
                try:
                    payload = json.loads(request.body.decode('utf-8'))
                except (ValueError, UnicodeDecodeError):
                    return JsonResponse({'detail': 'Некорректный JSON'}, status=400)
                array_endpoints = {
                    '/api/integration/erp/stock-update/',
                    '/api/integration/erp/catalog-sync/',
                }
                valid = (isinstance(payload, list) and all(isinstance(item, dict) for item in payload)
                         if request.path in array_endpoints else isinstance(payload, dict))
                if not valid:
                    return JsonResponse({'detail': 'Некорректная структура запроса'}, status=400)
        return self.get_response(request)
