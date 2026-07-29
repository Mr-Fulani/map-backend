from apps.ai_agent.models import AIModel, AITaskType, TenantAISettings


class AIModelRouter:
    """Выбирает модель тенанта и безопасные fallback-модели проекта."""

    @staticmethod
    def get_settings(tenant) -> TenantAISettings:
        defaults = {
            'default_model': next((
                model
                for model in AIModel.objects.filter(
                    is_active=True, is_default=True,
                ).order_by('sort_order')
                if model.is_selectable
            ), None),
        }
        settings_obj, _ = TenantAISettings.objects.get_or_create(
            tenant=tenant,
            defaults=defaults,
        )
        if settings_obj.default_model_id is None:
            model = defaults['default_model'] or next(iter(
                AIModelRouter._active_for_task(AITaskType.DESCRIPTION)
            ), None)
            if model:
                settings_obj.default_model = model
                settings_obj.save(update_fields=['default_model', 'updated_at'])
        return settings_obj

    @classmethod
    def primary_model(cls, tenant, task_type: str = AITaskType.DESCRIPTION) -> AIModel:
        settings_obj = cls.get_settings(tenant)
        model = settings_obj.default_model
        if settings_obj.use_task_overrides:
            override = settings_obj.task_models.filter(
                task_type=task_type,
            ).select_related('model').first()
            if override:
                model = override.model
        if model is None or not model.is_selectable or not model.supports_task(task_type):
            model = next(iter(cls._active_for_task(task_type)), None)
        if model is None:
            raise RuntimeError(f'Нет активной AI-модели для задачи {task_type}.')
        return model

    @classmethod
    def candidates(cls, tenant, task_type: str = AITaskType.DESCRIPTION) -> list[AIModel]:
        primary = cls.primary_model(tenant, task_type)
        active = cls._active_for_task(task_type)
        fallbacks = [model for model in active if model.pk != primary.pk and model.is_fallback]
        fallbacks.sort(key=lambda model: (
            model.provider == primary.provider,
            model.sort_order,
        ))
        return [primary, *fallbacks]

    @staticmethod
    def _active_for_task(task_type: str) -> list[AIModel]:
        return [
            model
            for model in AIModel.objects.filter(is_active=True).order_by('sort_order')
            if model.is_selectable and model.supports_task(task_type)
        ]
