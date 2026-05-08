from rest_framework.authentication import BaseAuthentication


class APIKeyAuthentication(BaseAuthentication):
    """
    Аутентификация по API Key из заголовка Authorization: Bearer <key>.
    Реализуется в Этапе 1.
    """

    def authenticate(self, request):
        # Заглушка — полная реализация в Этапе 1
        return None
