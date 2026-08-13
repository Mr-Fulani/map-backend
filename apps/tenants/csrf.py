from django.http import JsonResponse
from django.views.csrf import csrf_failure as django_csrf_failure


def csrf_failure(request, reason=''):
    """Return a stable API error while preserving Django's HTML for admin pages."""
    if not request.path.startswith('/api/'):
        return django_csrf_failure(request, reason=reason)

    response = JsonResponse(
        {
            'status': 'error',
            'code': 'csrf_failed',
            'message': 'CSRF-проверка не пройдена. Получите новый CSRF-токен.',
        },
        status=403,
    )
    response['Cache-Control'] = 'no-store'
    response['Pragma'] = 'no-cache'
    return response
