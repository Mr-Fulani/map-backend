from django.db import transaction
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from apps.tenants.api_views import AIAPIView as APIView

from apps.ai_agent.models import (
    AIModel, AIRequestLog, AITaskType, TenantAITaskModel,
)
from apps.ai_agent.routing import AIModelRouter
from apps.ai_agent.serializers import AIModelSerializer, AIRequestLogSerializer
from apps.billing.ai_wallet import AIWalletService
from apps.tenants.models import TenantUser


_AI_MODEL_LIST_RESPONSE = inline_serializer(
    name='AIModelListResponse',
    fields={
        'status': serializers.CharField(),
        'data': AIModelSerializer(many=True),
    },
)
_AI_SETTINGS_REQUEST = inline_serializer(
    name='AISettingsUpdateRequest',
    fields={
        'default_model': serializers.IntegerField(),
        'use_task_overrides': serializers.BooleanField(required=False),
        'task_models': serializers.DictField(
            child=serializers.IntegerField(), required=False,
        ),
    },
)
_AI_SETTINGS_RESPONSE = inline_serializer(
    name='AISettingsResponse',
    fields={
        'status': serializers.CharField(),
        'data': inline_serializer(
            name='AISettingsData',
            fields={
                'default_model': serializers.IntegerField(allow_null=True),
                'use_task_overrides': serializers.BooleanField(),
                'task_models': serializers.DictField(
                    child=serializers.IntegerField(),
                ),
                'tasks': inline_serializer(
                    name='AITaskAvailability',
                    fields={
                        'value': serializers.CharField(),
                        'label': serializers.CharField(),
                        'implemented': serializers.BooleanField(),
                    },
                    many=True,
                ),
                'wallet': serializers.DictField(
                    child=serializers.JSONField(),
                    help_text='Баланс, резервы и лимиты AI-кредитов.',
                ),
            },
        ),
    },
)
_AI_USAGE_LIST_RESPONSE = inline_serializer(
    name='AIUsageListResponse',
    fields={
        'status': serializers.CharField(),
        'data': AIRequestLogSerializer(many=True),
    },
)


def _can_manage_ai(request) -> bool:
    if getattr(request.user, 'is_api_key', False):
        return False
    return TenantUser.objects.filter(
        tenant=request.tenant,
        user=request.user,
        role__in=(TenantUser.ROLE_OWNER, TenantUser.ROLE_ADMIN),
    ).exists()


@extend_schema(tags=['AI'])
class AIModelListView(APIView):
    permission_classes = [IsAuthenticated]
    api_key_enabled = True

    @extend_schema(
        summary='Список доступных AI-моделей',
        responses=_AI_MODEL_LIST_RESPONSE,
    )
    def get(self, request):
        models = AIModel.objects.all().order_by('sort_order', 'display_name')
        return Response({
            'status': 'ok',
            'data': AIModelSerializer(models, many=True).data,
        })


@extend_schema(tags=['AI'])
class AISettingsView(APIView):
    permission_classes = [IsAuthenticated]
    api_key_scopes = {}

    @extend_schema(
        summary='Получить настройки AI-моделей',
        responses=_AI_SETTINGS_RESPONSE,
    )
    def get(self, request):
        return Response({'status': 'ok', 'data': self._data(request.tenant)})

    @extend_schema(
        summary='Обновить настройки AI-моделей',
        request=_AI_SETTINGS_REQUEST,
        responses=_AI_SETTINGS_RESPONSE,
    )
    def patch(self, request):
        if not _can_manage_ai(request):
            return Response(
                {
                    'status': 'error',
                    'code': 'forbidden',
                    'message': 'Изменять AI-модели может владелец или администратор.',
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        settings_obj = AIModelRouter.get_settings(request.tenant)
        default_model_id = request.data.get('default_model')
        use_task_overrides = bool(request.data.get('use_task_overrides', False))
        task_models = request.data.get('task_models') or {}
        if not isinstance(task_models, dict):
            return Response(
                {
                    'status': 'error',
                    'code': 'invalid_task_models',
                    'message': 'Настройки моделей по задачам должны быть объектом.',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            default_model = AIModel.objects.get(pk=default_model_id)
        except (AIModel.DoesNotExist, TypeError, ValueError):
            return Response(
                {
                    'status': 'error',
                    'code': 'invalid_model',
                    'message': 'Выбранная модель недоступна.',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not default_model.is_selectable:
            return Response(
                {
                    'status': 'error',
                    'code': 'model_unavailable',
                    'message': default_model.availability_reason,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        validated_task_models = {}
        if use_task_overrides:
            valid_tasks = {value for value, _label in AITaskType.choices}
            for task_type, model_id in task_models.items():
                if task_type not in valid_tasks:
                    continue
                try:
                    model = AIModel.objects.get(pk=model_id)
                except (AIModel.DoesNotExist, TypeError, ValueError):
                    return Response(
                        {
                            'status': 'error',
                            'code': 'invalid_task_model',
                            'message': f'Модель для задачи {task_type} недоступна.',
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if not model.is_selectable:
                    return Response(
                        {
                            'status': 'error',
                            'code': 'task_model_unavailable',
                            'message': (
                                f'{model.display_name}: {model.availability_reason}'
                            ),
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if not model.supports_task(task_type):
                    return Response(
                        {
                            'status': 'error',
                            'code': 'unsupported_task',
                            'message': f'{model.display_name} не поддерживает эту задачу.',
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                validated_task_models[task_type] = model

        with transaction.atomic():
            settings_obj.default_model = default_model
            settings_obj.use_task_overrides = use_task_overrides
            settings_obj.save(update_fields=[
                'default_model', 'use_task_overrides', 'updated_at',
            ])
            for task_type, model in validated_task_models.items():
                TenantAITaskModel.objects.update_or_create(
                    settings=settings_obj,
                    task_type=task_type,
                    defaults={'model': model},
                )

        return Response({'status': 'ok', 'data': self._data(request.tenant)})

    @staticmethod
    def _data(tenant):
        settings_obj = AIModelRouter.get_settings(tenant)
        overrides = {
            row.task_type: row.model_id
            for row in settings_obj.task_models.all()
        }
        wallet = AIWalletService.summary(tenant)
        return {
            'default_model': settings_obj.default_model_id,
            'use_task_overrides': settings_obj.use_task_overrides,
            'task_models': overrides,
            'tasks': [
                {
                    'value': value,
                    'label': label,
                    # Сейчас единственный LLM-потребитель проекта — DescriptionAgent.
                    # Остальные пайплайны детерминированные и не должны создавать
                    # ложное впечатление, что смена модели уже влияет на них.
                    'implemented': value == AITaskType.DESCRIPTION,
                }
                for value, label in AITaskType.choices
            ],
            'wallet': {
                key: str(value) if hasattr(value, 'as_tuple') else value
                for key, value in wallet.items()
            },
        }


@extend_schema(tags=['AI'])
class AIUsageListView(APIView):
    permission_classes = [IsAuthenticated]
    api_key_enabled = True

    @extend_schema(
        summary='История использования AI',
        responses=_AI_USAGE_LIST_RESPONSE,
    )
    def get(self, request):
        logs = AIRequestLog.objects.filter(
            tenant=request.tenant,
        ).order_by('-created_at')[:100]
        return Response({
            'status': 'ok',
            'data': AIRequestLogSerializer(logs, many=True).data,
        })
