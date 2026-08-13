from rest_framework.views import APIView


class ScopedAPIKeyAPIView(APIView):
    """Explicit machine-access opt-in; raw APIView remains API-key denied."""

    api_key_enabled = False
    api_key_scopes: dict[str, set[str]] = {}


class CatalogAPIView(ScopedAPIKeyAPIView):
    api_key_scopes = {
        'GET': {'catalog:read'},
        'HEAD': {'catalog:read'},
        'OPTIONS': {'catalog:read'},
        'POST': {'catalog:write'},
        'PUT': {'catalog:write'},
        'PATCH': {'catalog:write'},
        'DELETE': {'catalog:write'},
    }


class ListingsAPIView(ScopedAPIKeyAPIView):
    api_key_scopes = {
        'GET': {'listings:read'},
        'HEAD': {'listings:read'},
        'OPTIONS': {'listings:read'},
        'POST': {'listings:write'},
        'PUT': {'listings:write'},
        'PATCH': {'listings:write'},
        'DELETE': {'listings:write'},
    }


class MediaAPIView(ScopedAPIKeyAPIView):
    api_key_scopes = {
        'GET': {'media:read'},
        'HEAD': {'media:read'},
        'OPTIONS': {'media:read'},
        'POST': {'media:write'},
        'PUT': {'media:write'},
        'PATCH': {'media:write'},
        'DELETE': {'media:write'},
    }


class ResearchAPIView(ScopedAPIKeyAPIView):
    api_key_scopes = {
        'GET': {'research:read'},
        'HEAD': {'research:read'},
        'OPTIONS': {'research:read'},
        'POST': {'research:run'},
    }


class AIAPIView(ScopedAPIKeyAPIView):
    api_key_scopes = {
        'GET': {'ai:read'},
        'HEAD': {'ai:read'},
        'OPTIONS': {'ai:read'},
        'POST': {'ai:run'},
    }
