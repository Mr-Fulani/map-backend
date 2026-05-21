import logging

from rest_framework.views import exception_handler
from rest_framework.response import Response
from rest_framework import status

logger = logging.getLogger(__name__)


def map_exception_handler(exc, context):
    """Единый формат ошибок MAP API."""
    response = exception_handler(exc, context)

    if response is None:
        logger.exception('Unhandled exception in %s', context.get('view'))
        return Response(
            {'status': 'error', 'code': 'internal_error', 'message': 'Внутренняя ошибка сервера'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    if response.status_code == 400:
        return Response(
            {'status': 'error', 'code': 'validation_error', 'errors': response.data},
            status=response.status_code,
        )

    code_map = {
        401: 'unauthorized',
        403: 'forbidden',
        404: 'not_found',
        429: 'rate_limit_exceeded',
    }
    code = code_map.get(response.status_code, 'error')
    message = response.data.get('detail', str(response.data)) if isinstance(response.data, dict) else str(response.data)

    return Response(
        {'status': 'error', 'code': code, 'message': message},
        status=response.status_code,
    )
