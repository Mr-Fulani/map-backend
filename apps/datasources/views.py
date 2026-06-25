import hashlib
import os
import tempfile

from rest_framework import status
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.datasources.adapters.csv_adapter import CSVAdapter, CSVValidationError
from apps.datasources.models import DataSourceConnection
from apps.datasources.serializers import DataSourceConnectionSerializer, DataSourceConnectionUpdateSerializer
from apps.datasources.services import ConnectionService


class DataSourceListView(APIView):
    def get(self, request):
        qs = DataSourceConnection.objects.filter(tenant=request.tenant)
        return Response(DataSourceConnectionSerializer(qs, many=True).data)

    def post(self, request):
        serializer = DataSourceConnectionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        conn = ConnectionService.create(request.tenant, serializer.validated_data)
        return Response(DataSourceConnectionSerializer(conn).data, status=status.HTTP_201_CREATED)


class DataSourceDetailView(APIView):
    def get(self, request, pk):
        conn = DataSourceConnection.objects.get(pk=pk, tenant=request.tenant)
        return Response(DataSourceConnectionSerializer(conn).data)

    def put(self, request, pk):
        serializer = DataSourceConnectionUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        conn = ConnectionService.update(pk, request.tenant, serializer.validated_data)
        return Response(DataSourceConnectionSerializer(conn).data)

    def delete(self, request, pk):
        ConnectionService.delete(pk, request.tenant)
        return Response(status=status.HTTP_204_NO_CONTENT)


class DataSourceTestView(APIView):
    def post(self, request, pk):
        result = ConnectionService.test(pk, request.tenant)
        http_status = status.HTTP_200_OK if result['ok'] else status.HTTP_502_BAD_GATEWAY
        return Response(result, status=http_status)


class DataSourceSyncView(APIView):
    """POST /api/v1/datasources/{pk}/sync/ — запустить импорт в Celery."""

    def post(self, request, pk):
        from apps.datasources.models import DataSourceConnection
        from apps.products.tasks import import_from_datasource

        try:
            conn = DataSourceConnection.objects.get(pk=pk, tenant=request.tenant)
        except DataSourceConnection.DoesNotExist:
            return Response(
                {'status': 'error', 'code': 'not_found', 'message': 'Источник данных не найден'},
                status=status.HTTP_404_NOT_FOUND,
            )

        import_from_datasource.delay(conn.pk)
        return Response({'status': 'ok', 'message': 'Синхронизация запущена'})


class CSVUploadView(APIView):
    parser_classes = [MultiPartParser]

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
