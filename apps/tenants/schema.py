from drf_spectacular.extensions import OpenApiAuthenticationExtension


class APIKeyAuthenticationScheme(OpenApiAuthenticationExtension):
    """OpenAPI representation of MAP API keys passed as Bearer tokens."""

    target_class = 'apps.tenants.authentication.APIKeyAuthentication'
    name = 'mapApiKey'
    priority = 1

    def get_security_definition(self, auto_schema):
        return {
            'type': 'http',
            'scheme': 'bearer',
            'bearerFormat': 'MAP API key',
            'description': 'Tenant API key in the form `Bearer map_sk_...`.',
        }


class TenantJWTAuthenticationScheme(OpenApiAuthenticationExtension):
    """OpenAPI representation of tenant-scoped SimpleJWT access tokens."""

    target_class = 'apps.tenants.authentication.TenantJWTAuthentication'
    name = 'tenantJwt'
    priority = 1

    def get_security_definition(self, auto_schema):
        return {
            'type': 'http',
            'scheme': 'bearer',
            'bearerFormat': 'JWT',
            'description': 'Tenant-scoped JWT access token.',
        }
