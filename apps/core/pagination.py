from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response


class MapPagination(PageNumberPagination):
    """Стандартный формат пагинации MAP API."""

    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 500

    def get_paginated_response(self, data):
        return Response({
            'status': 'ok',
            'data': data,
            'meta': {
                'total': self.page.paginator.count,
                'page': self.page.number,
                'page_size': self.get_page_size(self.request),
                'next': self.get_next_link(),
                'prev': self.get_previous_link(),
            },
        })
