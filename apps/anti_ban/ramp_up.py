from django.utils.timezone import localdate, now


# Расписание прогрева: (день с момента создания тенанта, макс. публикаций за день)
RAMP_UP_SCHEDULE = [
    (1,  100),
    (2,  250),
    (3,  500),
    (7,  2_000),
    (14, 10_000),
]
# После 30 дней — без ограничений
UNLIMITED_AFTER_DAYS = 30


class GradualRampUp:
    """
    Контролирует постепенный прогрев аккаунта после регистрации.

    Avito детектирует резкий рост публикаций как накрутку — нужен плавный старт.
    Лимит рассчитывается по tenant.created_at относительно текущей даты.
    """

    def get_daily_limit(self, tenant) -> int:
        """
        Возвращает максимальное число публикаций для тенанта на сегодня.

        После UNLIMITED_AFTER_DAYS дней ограничений нет (возвращает большое число).
        """
        days_since_creation = (now().date() - tenant.created_at.date()).days + 1

        if days_since_creation >= UNLIMITED_AFTER_DAYS:
            return 999_999  # Без ограничений

        limit = RAMP_UP_SCHEDULE[0][1]  # Минимум по умолчанию
        for threshold_day, max_count in RAMP_UP_SCHEDULE:
            if days_since_creation >= threshold_day:
                limit = max_count
        return limit

    def is_allowed(self, tenant, published_today: int) -> bool:
        """
        Проверяет, можно ли опубликовать ещё одно объявление сегодня.

        published_today — число уже опубликованных объявлений за текущий день.
        """
        return published_today < self.get_daily_limit(tenant)

    def get_published_today(self, tenant) -> int:
        """Считает число активных публикаций за сегодня из БД (по tenant, изолированно)."""
        from apps.marketplaces.models import Listing
        return Listing.objects.filter(
            tenant=tenant,
            status=Listing.STATUS_ACTIVE,
            published_at__date=localdate(),
        ).count()
