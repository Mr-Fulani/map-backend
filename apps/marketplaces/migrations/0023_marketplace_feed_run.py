import uuid

import django.db.models.deletion
from django.db import migrations, models


ACTIVE_STATES = (
    'preparing',
    'submit_unknown',
    'polling',
    'reporting',
    'retry_wait',
)
OWNERSHIP_STATES = (
    *ACTIVE_STATES,
    'outcome_uncertain',
)


class Migration(migrations.Migration):

    dependencies = [
        ('marketplaces', '0022_account_status_lifecycle_concurrent_index'),
    ]

    operations = [
        migrations.CreateModel(
            name='MarketplaceFeedRun',
            fields=[
                (
                    'id',
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлено')),
                (
                    'marketplace',
                    models.CharField(
                        editable=False,
                        max_length=50,
                        verbose_name='Маркетплейс на момент запуска',
                    ),
                ),
                (
                    'state',
                    models.CharField(
                        choices=[
                            ('preparing', 'Подготовка'),
                            ('submit_unknown', 'Результат отправки неизвестен'),
                            ('polling', 'Ожидание обработки'),
                            ('reporting', 'Получение отчёта'),
                            ('retry_wait', 'Ожидание повтора'),
                            ('succeeded', 'Завершено'),
                            ('failed', 'Ошибка'),
                            (
                                'outcome_uncertain',
                                'Результат отправки требует сверки',
                            ),
                            ('superseded', 'Заменено новым запуском'),
                            ('cancelled', 'Отменено'),
                        ],
                        default='preparing',
                        editable=False,
                        max_length=20,
                        verbose_name='Состояние',
                    ),
                ),
                (
                    'revision',
                    models.PositiveBigIntegerField(
                        default=0,
                        editable=False,
                        verbose_name='Ревизия состояния',
                    ),
                ),
                (
                    'account_identity_digest',
                    models.CharField(
                        editable=False,
                        max_length=64,
                        verbose_name='Отпечаток идентичности аккаунта',
                    ),
                ),
                (
                    'payload_sha256',
                    models.CharField(
                        blank=True,
                        editable=False,
                        max_length=64,
                        verbose_name='SHA-256 отправленного фида',
                    ),
                ),
                (
                    'provider_run_id',
                    models.CharField(
                        blank=True,
                        editable=False,
                        max_length=200,
                        null=True,
                        verbose_name='ID запуска у площадки',
                    ),
                ),
                (
                    'provider_predecessor_run_id',
                    models.CharField(
                        blank=True,
                        editable=False,
                        max_length=200,
                        null=True,
                        verbose_name='ID предыдущего запуска у площадки',
                    ),
                ),
                (
                    'submitted_at',
                    models.DateTimeField(
                        blank=True,
                        editable=False,
                        null=True,
                        verbose_name='Фид отправлен',
                    ),
                ),
                (
                    'provider_result_deadline_at',
                    models.DateTimeField(
                        blank=True,
                        editable=False,
                        null=True,
                        verbose_name='Крайний срок сверки результата площадки',
                    ),
                ),
                (
                    'submission_reconcile_attempt',
                    models.PositiveSmallIntegerField(
                        default=0,
                        editable=False,
                        verbose_name='Подтверждённых отрицательных сверок отправки',
                    ),
                ),
                (
                    'poll_cursor_listing_id',
                    models.PositiveBigIntegerField(
                        default=0,
                        editable=False,
                        verbose_name='Курсор проверки листингов',
                    ),
                ),
                (
                    'poll_round',
                    models.PositiveIntegerField(
                        default=0,
                        editable=False,
                        verbose_name='Раунд проверки',
                    ),
                ),
                (
                    'report_page',
                    models.PositiveIntegerField(
                        default=1,
                        editable=False,
                        verbose_name='Следующая страница отчёта',
                    ),
                ),
                (
                    'report_attempt',
                    models.PositiveSmallIntegerField(
                        default=0,
                        editable=False,
                        verbose_name='Попытка получения отчёта',
                    ),
                ),
                (
                    'report_completed_at',
                    models.DateTimeField(
                        blank=True,
                        editable=False,
                        null=True,
                        verbose_name='Отчёт площадки полностью обработан',
                    ),
                ),
                (
                    'next_attempt_at',
                    models.DateTimeField(
                        blank=True,
                        editable=False,
                        null=True,
                        verbose_name='Следующая попытка',
                    ),
                ),
                (
                    'claim_token',
                    models.UUIDField(
                        blank=True,
                        editable=False,
                        null=True,
                        verbose_name='Токен владельца запуска',
                    ),
                ),
                (
                    'claimed_until',
                    models.DateTimeField(
                        blank=True,
                        editable=False,
                        null=True,
                        verbose_name='Lease запуска истекает',
                    ),
                ),
                (
                    'total_count',
                    models.PositiveIntegerField(
                        default=0,
                        editable=False,
                        verbose_name='Всего листингов',
                    ),
                ),
                (
                    'published_count',
                    models.PositiveIntegerField(
                        default=0,
                        editable=False,
                        verbose_name='Опубликовано',
                    ),
                ),
                (
                    'rejected_count',
                    models.PositiveIntegerField(
                        default=0,
                        editable=False,
                        verbose_name='Отклонено',
                    ),
                ),
                (
                    'pending_count',
                    models.PositiveIntegerField(
                        default=0,
                        editable=False,
                        verbose_name='Ожидает обработки',
                    ),
                ),
                (
                    'last_error',
                    models.TextField(
                        blank=True,
                        editable=False,
                        max_length=2000,
                        verbose_name='Последняя ошибка',
                    ),
                ),
                (
                    'finished_at',
                    models.DateTimeField(
                        blank=True,
                        editable=False,
                        null=True,
                        verbose_name='Завершено',
                    ),
                ),
                (
                    'account',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='feed_runs',
                        to='marketplaces.marketplaceaccount',
                        verbose_name='Аккаунт маркетплейса',
                    ),
                ),
                (
                    'tenant',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='marketplace_feed_runs',
                        to='tenants.tenant',
                        verbose_name='Тенант',
                    ),
                ),
            ],
            options={
                'verbose_name': 'Запуск фида маркетплейса',
                'verbose_name_plural': 'Запуски фидов маркетплейсов',
                'indexes': [
                    models.Index(
                        condition=models.Q(
                            state__in=ACTIVE_STATES,
                            next_attempt_at__isnull=False,
                        ),
                        fields=['marketplace', 'next_attempt_at', 'id'],
                        name='mkt_feed_due_idx',
                    ),
                ],
                'constraints': [
                    models.UniqueConstraint(
                        condition=models.Q(state__in=OWNERSHIP_STATES),
                        fields=('account',),
                        name='uniq_mkt_feed_owner_account',
                    ),
                    models.UniqueConstraint(
                        condition=(
                            models.Q(provider_run_id__isnull=False)
                            & ~models.Q(provider_run_id='')
                        ),
                        fields=('account', 'provider_run_id'),
                        name='uniq_mkt_feed_provider_ref',
                    ),
                ],
            },
        ),
        migrations.AddField(
            model_name='listing',
            name='feed_run',
            field=models.ForeignKey(
                blank=True,
                db_index=False,
                editable=False,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='listings',
                to='marketplaces.marketplacefeedrun',
                verbose_name='Поколение фида',
            ),
        ),
    ]
