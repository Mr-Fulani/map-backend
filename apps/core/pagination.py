from collections.abc import Sequence
from typing import Any, TypeVar, cast

from rest_framework.request import Request
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response


_T = TypeVar('_T')


class MapPagination(PageNumberPagination):
    """Стандартный формат пагинации MAP API."""

    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 500

    def paginate_sequence(self, values: Sequence[_T], request: Request) -> list[_T]:
        """Paginate an in-memory sequence using DRF's supported paginator path.

        DRF accepts sequences at runtime through Django's ``Paginator`` while
        the third-party type stub currently narrows the input to ``QuerySet``.
        Keep that compatibility cast contained at this boundary.
        """
        page = super().paginate_queryset(cast(Any, values), request)
        return page or []

    def get_paginated_response(self, data: Any) -> Response:
        page = self.page
        request = self.request
        if page is None or request is None:
            raise RuntimeError('paginate_queryset must run before building a response')
        return Response({
            'status': 'ok',
            'data': data,
            'meta': {
                'total': page.paginator.count,
                'page': page.number,
                'page_size': self.get_page_size(request),
                'next': self.get_next_link(),
                'prev': self.get_previous_link(),
            },
        })
