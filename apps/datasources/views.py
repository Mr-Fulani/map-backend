import hashlib
import os
import tempfile

from drf_spectacular.utils import (
    OpenApiParameter, OpenApiTypes, extend_schema, inline_serializer,
)
from rest_framework import serializers, status
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.datasources.adapters.csv_adapter import CSVAdapter, CSVValidationError
from apps.datasources.models import DataSourceConnection
from apps.datasources.serializers import DataSourceConnectionSerializer, DataSourceConnectionUpdateSerializer
from apps.datasources.services import ConnectionService
from apps.tenants.permissions import TenantAdminPermission, TenantAdminWritePermission


_CONNECTION_TEST_RESPONSE = inline_serializer(
    name='DataSourceConnectionTestResponse',
    fields={
        'ok': serializers.BooleanField(),
        'error': serializers.CharField(required=False),
    },
)
_SYNC_RESPONSE = inline_serializer(
    name='DataSourceSyncResponse',
    fields={
        'status': serializers.CharField(),
        'message': serializers.CharField(),
    },
)
_CSV_UPLOAD_REQUEST = inline_serializer(
    name='DataSourceCSVUploadRequest',
    fields={'file': serializers.FileField()},
)
_CSV_UPLOAD_RESPONSE = inline_serializer(
    name='DataSourceCSVUploadResponse',
    fields={
        'data': serializers.DictField(
            child=serializers.JSONField(),
            required=False,
            help_text='Результат импорта или предпросмотра файла.',
        ),
        'headers': serializers.ListField(
            child=serializers.CharField(), required=False,
        ),
        'rows': serializers.ListField(
            child=serializers.DictField(), required=False,
        ),
        'total_rows': serializers.IntegerField(required=False),
    },
)


@extend_schema(tags=['Data sources'])
class DataSourceListView(APIView):
    permission_classes = [IsAuthenticated, TenantAdminWritePermission]

    @extend_schema(
        summary='Список источников данных',
        responses=DataSourceConnectionSerializer(many=True),
    )
    def get(self, request):
        qs = DataSourceConnection.objects.filter(tenant=request.tenant)
        return Response(DataSourceConnectionSerializer(qs, many=True).data)

    @extend_schema(
        summary='Создать источник данных',
        request=DataSourceConnectionSerializer,
        responses={201: DataSourceConnectionSerializer},
    )
    def post(self, request):
        serializer = DataSourceConnectionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        conn = ConnectionService.create(request.tenant, serializer.validated_data)
        return Response(DataSourceConnectionSerializer(conn).data, status=status.HTTP_201_CREATED)


@extend_schema(tags=['Data sources'])
class DataSourceDetailView(APIView):
    permission_classes = [IsAuthenticated, TenantAdminWritePermission]

    @extend_schema(
        summary='Получить источник данных',
        responses=DataSourceConnectionSerializer,
    )
    def get(self, request, pk: int):
        conn = DataSourceConnection.objects.get(pk=pk, tenant=request.tenant)
        return Response(DataSourceConnectionSerializer(conn).data)

    @extend_schema(
        summary='Обновить источник данных',
        request=DataSourceConnectionUpdateSerializer,
        responses=DataSourceConnectionSerializer,
    )
    def put(self, request, pk: int):
        serializer = DataSourceConnectionUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        conn = ConnectionService.update(pk, request.tenant, serializer.validated_data)
        return Response(DataSourceConnectionSerializer(conn).data)

    @extend_schema(
        summary='Удалить источник данных',
        request=None,
        responses={204: None},
    )
    def delete(self, request, pk: int):
        ConnectionService.delete(pk, request.tenant)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Data sources'])
class DataSourceTestView(APIView):
    permission_classes = [IsAuthenticated, TenantAdminPermission]

    @extend_schema(
        summary='Проверить подключение к источнику',
        request=None,
        responses=_CONNECTION_TEST_RESPONSE,
    )
    def post(self, request, pk: int):
        result = ConnectionService.test(pk, request.tenant)
        http_status = status.HTTP_200_OK if result['ok'] else status.HTTP_502_BAD_GATEWAY
        return Response(result, status=http_status)


@extend_schema(tags=['Data sources'])
class DataSourceSyncView(APIView):
    """POST /api/v1/datasources/{pk}/sync/ — запустить импорт в Celery."""

    permission_classes = [IsAuthenticated, TenantAdminPermission]

    @extend_schema(
        summary='Запустить синхронизацию источника',
        request=None,
        responses=_SYNC_RESPONSE,
    )
    def post(self, request, pk: int):
        from apps.datasources.models import DataSourceConnection
        from apps.products.tasks import import_from_datasource

        try:
            conn = DataSourceConnection.objects.get(
                pk=pk,
                tenant=request.tenant,
                is_active=True,
            )
        except DataSourceConnection.DoesNotExist:
            return Response(
                {'status': 'error', 'code': 'not_found', 'message': 'Источник данных не найден'},
                status=status.HTTP_404_NOT_FOUND,
            )

        import_from_datasource.delay(conn.pk)
        return Response({'status': 'ok', 'message': 'Синхронизация запущена'})


@extend_schema(tags=['Data sources'])
class CSVUploadView(APIView):
    parser_classes = [MultiPartParser]
    permission_classes = [IsAuthenticated, TenantAdminPermission]

    @extend_schema(
        summary='Загрузить CSV или Excel',
        request=_CSV_UPLOAD_REQUEST,
        parameters=[
            OpenApiParameter(
                name='preview',
                type=OpenApiTypes.BOOL,
                location=OpenApiParameter.QUERY,
                description='Вернуть предпросмотр без импорта.',
            ),
            OpenApiParameter(
                name='confirm',
                type=OpenApiTypes.BOOL,
                location=OpenApiParameter.QUERY,
                description='Подтвердить повторную загрузку файла.',
            ),
        ],
        responses=_CSV_UPLOAD_RESPONSE,
    )
    def post(self, request):
        file_obj = request.FILES.get('file')
        if file_obj is None:
            return Response({'detail': 'Файл обязателен.'}, status=status.HTTP_400_BAD_REQUEST)

        suffix = os.path.splitext(file_obj.name)[1]
        hasher = hashlib.sha256()
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            for chunk in file_obj.chunks():
                hasher.update(chunk)
                tmp.write(chunk)
            tmp_path = tmp.name
        content_hash = hasher.hexdigest()

        preview = request.query_params.get('preview') == '1'
        confirm = request.query_params.get('confirm') in ('1', 'true', 'True')

        try:
            adapter = CSVAdapter(connection=None)
            if preview:
                result = adapter.preview(tmp_path)
            else:
                if not confirm:
                    duplicate = ConnectionService.find_duplicate_upload(
                        request.tenant, content_hash, file_obj.name,
                    )
                    if duplicate:
                        return Response(
                            {'status': 'duplicate', **duplicate},
                            status=status.HTTP_409_CONFLICT,
                        )

                items = adapter.process_uploaded_file(tmp_path)
                result_data = ConnectionService.process_csv_upload(
                    tenant=request.tenant,
                    file_name=file_obj.name,
                    items=items,
                    content_hash=content_hash,
                )

                result = {
                    'data': result_data
                }
        except CSVValidationError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        finally:
            os.unlink(tmp_path)

        return Response(result)
