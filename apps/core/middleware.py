class TenantMiddleware:
    """
    Мультитенантный middleware — определяет тенанта по API Key или поддомену.
    Реализуется в Этапе 1.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Заглушка — полная реализация в apps/core/middleware.py (Этап 1)
        request.tenant = None
        return self.get_response(request)
