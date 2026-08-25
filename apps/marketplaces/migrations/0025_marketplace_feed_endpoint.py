import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('marketplaces', '0024_feed_run_listing_concurrent_index'),
    ]

    operations = [
        migrations.CreateModel(
            name='MarketplaceFeedEndpoint',
            fields=[
                (
                    'public_id',
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                        verbose_name='Публичный ID stable feed endpoint',
                    ),
                ),
                (
                    'created_at',
                    models.DateTimeField(auto_now_add=True, verbose_name='Создано'),
                ),
                (
                    'updated_at',
                    models.DateTimeField(auto_now=True, verbose_name='Обновлено'),
                ),
                (
                    'token_key_id',
                    models.CharField(
                        editable=False,
                        max_length=32,
                        verbose_name='ID HMAC-ключа capability token',
                    ),
                ),
                (
                    'previous_token_key_id',
                    models.CharField(
                        blank=True,
                        editable=False,
                        max_length=32,
                        verbose_name=(
                            'Предыдущий ID HMAC-ключа на время ротации'
                        ),
                    ),
                ),
                (
                    'owner_identity_digest',
                    models.CharField(
                        editable=False,
                        max_length=64,
                        verbose_name=(
                            'Отпечаток provider identity владельца'
                        ),
                    ),
                ),
                (
                    'capability_revision',
                    models.PositiveBigIntegerField(
                        default=1,
                        editable=False,
                        verbose_name='Ревизия capability token',
                    ),
                ),
                (
                    'serve_enabled',
                    models.BooleanField(
                        default=False,
                        editable=False,
                        verbose_name='Публичная выдача разрешена',
                    ),
                ),
                (
                    'storage_mode',
                    models.CharField(
                        choices=[
                            ('legacy_bridge', 'Мост к legacy-фиду'),
                            ('private_generation', 'Приватные поколения фида'),
                        ],
                        default='legacy_bridge',
                        editable=False,
                        max_length=24,
                        verbose_name='Режим хранения фида',
                    ),
                ),
                (
                    'legacy_object_key',
                    models.CharField(
                        blank=True,
                        editable=False,
                        max_length=1024,
                        verbose_name='Замороженный legacy object key',
                    ),
                ),
                (
                    'legacy_profile_url',
                    models.URLField(
                        blank=True,
                        editable=False,
                        max_length=2048,
                        verbose_name='Точный legacy URL в профиле площадки',
                    ),
                ),
                (
                    'profile_state',
                    models.CharField(
                        choices=[
                            ('new', 'Создан'),
                            ('bridge_ready', 'Legacy-мост готов'),
                            ('migrating', 'Профиль переводится'),
                            ('update_unknown', 'Результат обновления неизвестен'),
                            ('verified', 'Stable URL подтверждён'),
                            ('manual_review', 'Требуется ручная сверка'),
                        ],
                        default='new',
                        editable=False,
                        max_length=20,
                        verbose_name='Состояние миграции профиля',
                    ),
                ),
                (
                    'profile_fingerprint',
                    models.CharField(
                        blank=True,
                        editable=False,
                        max_length=64,
                        verbose_name='SHA-256 последнего проверенного профиля',
                    ),
                ),
                (
                    'profile_revision',
                    models.PositiveBigIntegerField(
                        default=0,
                        editable=False,
                        verbose_name='Ревизия состояния профиля',
                    ),
                ),
                (
                    'profile_verified_at',
                    models.DateTimeField(
                        blank=True,
                        editable=False,
                        null=True,
                        verbose_name='Профиль площадки проверен',
                    ),
                ),
                (
                    'account',
                    models.OneToOneField(
                        editable=False,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='feed_endpoint',
                        to='marketplaces.marketplaceaccount',
                        verbose_name='Аккаунт маркетплейса',
                    ),
                ),
            ],
            options={
                'verbose_name': 'Stable feed endpoint маркетплейса',
                'verbose_name_plural': 'Stable feed endpoints маркетплейсов',
                'indexes': [
                    models.Index(
                        fields=['profile_state', 'updated_at', 'public_id'],
                        name='mkt_feed_ep_state_updated',
                    ),
                ],
                'constraints': [
                    models.CheckConstraint(
                        condition=models.Q(
                            token_key_id__regex=(
                                r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$'
                            ),
                        ),
                        name='mkt_feed_ep_key_id_format',
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(previous_token_key_id='')
                            | (
                                models.Q(
                                    previous_token_key_id__regex=(
                                        r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$'
                                    ),
                                )
                                & ~models.Q(
                                    previous_token_key_id=models.F('token_key_id'),
                                )
                            )
                        ),
                        name='mkt_feed_ep_prev_key',
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(previous_token_key_id='')
                            | models.Q(
                                profile_state__in=('migrating', 'update_unknown'),
                            )
                        ),
                        name='mkt_feed_ep_prev_key_state',
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            owner_identity_digest__regex=r'^[0-9a-f]{64}$',
                        ),
                        name='mkt_feed_ep_owner_digest',
                    ),
                    models.CheckConstraint(
                        condition=models.Q(capability_revision__gte=1),
                        name='mkt_feed_ep_cap_revision',
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            storage_mode__in=(
                                'legacy_bridge',
                                'private_generation',
                            ),
                        ),
                        name='mkt_feed_ep_storage_mode',
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            profile_state__in=(
                                'new',
                                'bridge_ready',
                                'migrating',
                                'update_unknown',
                                'verified',
                                'manual_review',
                            ),
                        ),
                        name='mkt_feed_ep_profile_state',
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(legacy_object_key='', legacy_profile_url='')
                            | (
                                ~models.Q(legacy_object_key='')
                                & models.Q(legacy_profile_url__startswith='https://')
                            )
                        ),
                        name='mkt_feed_ep_legacy_bundle',
                    ),
                    models.CheckConstraint(
                        condition=(
                            ~models.Q(
                                profile_state__in=(
                                    'bridge_ready',
                                    'migrating',
                                    'update_unknown',
                                    'verified',
                                ),
                            )
                            | ~models.Q(legacy_object_key='')
                        ),
                        name='mkt_feed_ep_state_legacy',
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(
                                profile_fingerprint='',
                                profile_verified_at__isnull=True,
                            )
                            | models.Q(
                                profile_fingerprint__regex=r'^[0-9a-f]{64}$',
                                profile_verified_at__isnull=False,
                            )
                        ),
                        name='mkt_feed_ep_profile_baseline',
                    ),
                    models.CheckConstraint(
                        condition=(
                            ~models.Q(
                                profile_state__in=(
                                    'bridge_ready',
                                    'migrating',
                                    'update_unknown',
                                    'verified',
                                ),
                            )
                            | models.Q(
                                profile_fingerprint__regex=r'^[0-9a-f]{64}$',
                                profile_verified_at__isnull=False,
                            )
                        ),
                        name='mkt_feed_ep_servable_baseline',
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(serve_enabled=False)
                            | (
                                models.Q(
                                    profile_state__in=(
                                        'bridge_ready',
                                        'migrating',
                                        'update_unknown',
                                        'verified',
                                    ),
                                )
                                & ~models.Q(legacy_object_key='')
                            )
                        ),
                        name='mkt_feed_ep_serve_guard',
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(storage_mode='legacy_bridge')
                            | models.Q(serve_enabled=False)
                        ),
                        name='mkt_feed_ep_private_dark',
                    ),
                ],
            },
        ),
    ]
