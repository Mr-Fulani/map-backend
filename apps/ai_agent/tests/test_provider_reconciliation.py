import json
import threading
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from queue import Queue

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connection, transaction
from django.db.models.deletion import ProtectedError
from django.utils import timezone
from django_celery_beat.models import PeriodicTask

from apps.ai_agent.models import AIProviderOperation, AITaskType
from apps.ai_agent.reconciliation import (
    AIProviderReconciliationRequired, begin_ai_provider_operation,
    mark_ai_provider_network_started,
    mark_ai_provider_operation_uncertain,
    reconcile_stale_ai_provider_operations, release_ai_provider_operation,
    settle_ai_provider_operation,
)
from apps.billing.ai_wallet import AIWalletService
from apps.tenants.services import TenantService


def make_tenant(slug):
    tenant, _ = TenantService.create_tenant(
        slug, slug, f'{slug}@test.com', 'pass12345',
    )
    return tenant


def make_uncertain_operation(slug='reconcile-ai-operation'):
    from apps.products.models import Product

    tenant = make_tenant(slug)
    product = Product.objects.create(
        tenant=tenant,
        article=f'{slug}-product',
        name='AI reconciliation product',
        price='1.00',
    )
    operation = begin_ai_provider_operation(
        tenant=tenant,
        task_type=AITaskType.DESCRIPTION,
        provider='openai',
        model_id='test-model',
        reserved_amount=Decimal('1.2500'),
        domain_type=AIProviderOperation.DomainType.PRODUCT,
        domain_reference=str(product.pk),
        reservation_details={'test': True},
    )
    mark_ai_provider_operation_uncertain(
        operation.pk,
        error_code='connection_error',
    )
    return tenant, operation


def run_command(operation, *, action, note='provider dashboard checked'):
    stdout = StringIO()
    call_command(
        'reconcile_ai_provider_operation',
        str(operation.pk),
        action=action,
        note=note,
        confirm=operation.pk,
        stdout=stdout,
    )
    return json.loads(stdout.getvalue())


@pytest.mark.django_db
def test_release_resolution_is_idempotent_and_keeps_audit_note():
    tenant, operation = make_uncertain_operation('reconcile-ai-release')
    assert AIWalletService.summary(tenant)['reserved'] == Decimal('1.2500')

    first = run_command(operation, action='release')
    second = run_command(operation, action='release', note='safe retry')

    operation.refresh_from_db()
    assert first['changed'] is True
    assert second['changed'] is False
    assert operation.status == AIProviderOperation.Status.RELEASED
    assert operation.resolution_action == 'release'
    assert operation.operator_note == 'provider dashboard checked'
    assert operation.released_at is not None
    assert operation.resolved_at is not None
    assert AIWalletService.summary(tenant)['reserved'] == 0
    assert tenant.ai_credit_transactions.filter(
        idempotency_key=f'{operation.reservation_key}:release',
    ).count() == 1


@pytest.mark.django_db
def test_settle_reserved_resolution_is_idempotent_and_charges_reserve():
    tenant, operation = make_uncertain_operation('reconcile-ai-settle')
    before = AIWalletService.summary(tenant)['total']

    first = run_command(operation, action='settle-reserved')
    second = run_command(operation, action='settle-reserved', note='safe retry')

    operation.refresh_from_db()
    assert first['changed'] is True
    assert second['changed'] is False
    assert operation.status == AIProviderOperation.Status.SETTLED
    assert operation.resolution_action == 'settle_reserved'
    assert operation.charged_amount == Decimal('1.2500')
    assert operation.operator_note == 'provider dashboard checked'
    assert operation.settled_at is not None
    assert operation.resolved_at is not None
    summary = AIWalletService.summary(tenant)
    assert summary['reserved'] == 0
    assert summary['total'] == before - Decimal('1.2500')
    assert tenant.ai_credit_transactions.filter(
        idempotency_key=f'{operation.reservation_key}:settled',
    ).count() == 1


@pytest.mark.django_db
def test_command_requires_exact_confirmation_and_nonempty_note():
    _, operation = make_uncertain_operation('reconcile-ai-confirmation')

    with pytest.raises(CommandError, match='совпадать'):
        call_command(
            'reconcile_ai_provider_operation',
            str(operation.pk),
            action='release',
            note='checked',
            confirm='00000000-0000-0000-0000-000000000000',
        )
    with pytest.raises(CommandError, match='пустым'):
        call_command(
            'reconcile_ai_provider_operation',
            str(operation.pk),
            action='release',
            note='   ',
            confirm=operation.pk,
        )

    operation.refresh_from_db()
    assert operation.status == AIProviderOperation.Status.PENDING_RECONCILIATION
    assert operation.resolved_at is None


@pytest.mark.django_db
def test_conflicting_terminal_resolution_is_rejected():
    _, operation = make_uncertain_operation('reconcile-ai-conflict')
    run_command(operation, action='release')

    with pytest.raises(CommandError, match='already terminal'):
        run_command(operation, action='settle-reserved')


@pytest.mark.django_db
def test_unresolved_domain_blocks_second_paid_operation_until_resolution():
    tenant, operation = make_uncertain_operation('reconcile-ai-domain-fence')

    with pytest.raises(
        AIProviderReconciliationRequired,
        match='provider_reconciliation_required',
    ):
        begin_ai_provider_operation(
            tenant=tenant,
            task_type=AITaskType.DESCRIPTION,
            provider='anthropic',
            model_id='second-model',
            reserved_amount=Decimal('1'),
            domain_type=AIProviderOperation.DomainType.PRODUCT,
            domain_reference=operation.domain_reference,
        )

    assert AIProviderOperation.objects.filter(tenant=tenant).count() == 1
    assert AIWalletService.summary(tenant)['reserved'] == Decimal('1.2500')

    release_ai_provider_operation(operation.pk, reason='manual_reconciliation')
    next_operation = begin_ai_provider_operation(
        tenant=tenant,
        task_type=AITaskType.DESCRIPTION,
        provider='anthropic',
        model_id='second-model',
        reserved_amount=Decimal('1'),
        domain_type=AIProviderOperation.DomainType.PRODUCT,
        domain_reference=operation.domain_reference,
    )

    assert next_operation.status == AIProviderOperation.Status.RESERVED
    assert AIProviderOperation.objects.filter(tenant=tenant).count() == 2


@pytest.mark.django_db
def test_unapplied_paid_result_blocks_second_domain_operation():
    from apps.products.models import Product

    tenant = make_tenant('reconcile-ai-unapplied-domain-fence')
    product = Product.objects.create(
        tenant=tenant,
        article='unapplied-domain-product',
        name='Unapplied domain product',
        price='1.00',
    )
    operation = begin_ai_provider_operation(
        tenant=tenant,
        task_type=AITaskType.DESCRIPTION,
        provider='openai',
        model_id='first-model',
        reserved_amount=Decimal('1'),
        domain_type=AIProviderOperation.DomainType.PRODUCT,
        domain_reference=str(product.pk),
    )
    settle_ai_provider_operation(
        operation.pk,
        actual_amount=Decimal('1'),
        validated_result={
            'title': 'Title',
            'description': 'Description',
            'confidence': 1.0,
        },
        apply_required=True,
    )

    with pytest.raises(AIProviderReconciliationRequired):
        begin_ai_provider_operation(
            tenant=tenant,
            task_type=AITaskType.DESCRIPTION,
            provider='anthropic',
            model_id='second-model',
            reserved_amount=Decimal('1'),
            domain_type=AIProviderOperation.DomainType.PRODUCT,
            domain_reference=str(product.pk),
        )

    assert AIProviderOperation.objects.filter(tenant=tenant).count() == 1


@pytest.mark.django_db
def test_reserved_operation_cannot_be_resolved_without_uncertainty_marker():
    from apps.products.models import Product

    tenant = make_tenant('reconcile-ai-not-uncertain')
    product = Product.objects.create(
        tenant=tenant,
        article='not-uncertain-product',
        name='Not uncertain product',
        price='1.00',
    )
    operation = begin_ai_provider_operation(
        tenant=tenant,
        task_type=AITaskType.DESCRIPTION,
        provider='openai',
        model_id='test-model',
        reserved_amount=Decimal('1'),
        domain_type=AIProviderOperation.DomainType.PRODUCT,
        domain_reference=str(product.pk),
    )

    with pytest.raises(CommandError, match='pending reconciliation'):
        run_command(operation, action='release')

    operation.refresh_from_db()
    assert operation.status == AIProviderOperation.Status.RESERVED
    assert AIWalletService.summary(tenant)['reserved'] == Decimal('1')


@pytest.mark.django_db
def test_stale_started_operation_is_held_for_manual_reconciliation():
    from apps.products.models import Product

    tenant = make_tenant('reconcile-ai-stale-started')
    product = Product.objects.create(
        tenant=tenant,
        article='stale-started-product',
        name='Stale started product',
        price='1.00',
    )
    operation = begin_ai_provider_operation(
        tenant=tenant,
        task_type=AITaskType.DESCRIPTION,
        provider='openai',
        model_id='test-model',
        reserved_amount=Decimal('2'),
        domain_type=AIProviderOperation.DomainType.PRODUCT,
        domain_reference=str(product.pk),
    )
    mark_ai_provider_network_started(operation.pk)
    checked_at = timezone.now()
    AIProviderOperation.objects.filter(pk=operation.pk).update(
        network_started_at=checked_at - timedelta(minutes=11),
    )

    result = reconcile_stale_ai_provider_operations(
        current_time=checked_at,
        started_timeout_seconds=600,
        never_started_timeout_seconds=300,
    )

    operation.refresh_from_db()
    assert result == {
        'pending_reconciliation': 1,
        'released_never_started': 0,
    }
    assert operation.status == AIProviderOperation.Status.PENDING_RECONCILIATION
    assert operation.provider_error_code == 'stale_provider_call'
    assert operation.uncertainty_marked_at == checked_at
    assert AIWalletService.summary(tenant)['reserved'] == Decimal('2')


@pytest.mark.django_db
def test_stale_never_started_operation_is_safely_released():
    from apps.products.models import Product

    tenant = make_tenant('reconcile-ai-stale-never-started')
    product = Product.objects.create(
        tenant=tenant,
        article='stale-never-started-product',
        name='Stale never started product',
        price='1.00',
    )
    operation = begin_ai_provider_operation(
        tenant=tenant,
        task_type=AITaskType.DESCRIPTION,
        provider='openai',
        model_id='test-model',
        reserved_amount=Decimal('3'),
        domain_type=AIProviderOperation.DomainType.PRODUCT,
        domain_reference=str(product.pk),
    )
    checked_at = timezone.now()
    AIProviderOperation.objects.filter(pk=operation.pk).update(
        created_at=checked_at - timedelta(minutes=6),
    )

    result = reconcile_stale_ai_provider_operations(
        current_time=checked_at,
        started_timeout_seconds=600,
        never_started_timeout_seconds=300,
    )

    operation.refresh_from_db()
    assert result == {
        'pending_reconciliation': 0,
        'released_never_started': 1,
    }
    assert operation.status == AIProviderOperation.Status.RELEASED
    assert operation.terminal_reason == 'provider_call_never_started'
    assert AIWalletService.summary(tenant)['reserved'] == 0


@pytest.mark.django_db
def test_periodic_setup_registers_stale_ai_operation_reconciler():
    call_command('setup_periodic_tasks', stdout=StringIO())

    task = PeriodicTask.objects.get(
        name='reconcile_stale_ai_provider_operations',
    )
    assert task.task == (
        'apps.ai_agent.tasks.reconcile_stale_ai_provider_operations_task'
    )
    assert task.queue == 'notifications'
    assert task.interval.every == 5
    assert task.interval.period == 'minutes'


@pytest.mark.django_db
def test_validated_result_has_a_hard_serialized_size_limit():
    from apps.products.models import Product

    tenant = make_tenant('reconcile-ai-result-bound')
    product = Product.objects.create(
        tenant=tenant,
        article='result-bound-product',
        name='Result bound product',
        price='1.00',
    )
    operation = begin_ai_provider_operation(
        tenant=tenant,
        task_type=AITaskType.DESCRIPTION,
        provider='openai',
        model_id='test-model',
        reserved_amount=Decimal('1'),
        domain_type=AIProviderOperation.DomainType.PRODUCT,
        domain_reference=str(product.pk),
    )
    mark_ai_provider_network_started(operation.pk)

    with pytest.raises(ValueError, match='256 KiB'):
        settle_ai_provider_operation(
            operation.pk,
            actual_amount=Decimal('1'),
            validated_result={'description': 'x' * (256 * 1024)},
            apply_required=True,
        )

    operation.refresh_from_db()
    assert operation.status == AIProviderOperation.Status.RESERVED
    assert operation.validated_result is None
    assert AIWalletService.summary(tenant)['reserved'] == Decimal('1')


@pytest.mark.django_db
def test_unresolved_operation_and_description_product_resist_hard_delete():
    from apps.products.models import Product

    tenant = make_tenant('ai-provider-delete-product')
    product = Product.objects.create(
        tenant=tenant,
        article='AI-DELETE-1',
        name='Protected paid result',
        price='1.00',
    )
    operation = AIProviderOperation.objects.create(
        tenant=tenant,
        task_type=AITaskType.DESCRIPTION,
        provider='openai',
        model_id='test-model',
        reservation_key='ai-delete-product-reservation',
        reserved_amount=Decimal('1'),
        domain_type=AIProviderOperation.DomainType.PRODUCT,
        domain_reference=str(product.pk),
        status=AIProviderOperation.Status.SETTLED,
        apply_state=AIProviderOperation.ApplyState.PENDING,
        validated_result={'title': 'Paid', 'description': 'Paid result'},
    )

    with pytest.raises(ProtectedError, match='unresolved AI provider'), \
         transaction.atomic():
        product.hard_delete()
    with pytest.raises(ProtectedError, match='unresolved AI provider'), \
         transaction.atomic():
        Product.all_objects.filter(pk=product.pk).delete()
    with pytest.raises(ProtectedError, match='Unresolved AI provider'), \
         transaction.atomic():
        operation.delete()
    with pytest.raises(ProtectedError, match='Unresolved AI provider'), \
         transaction.atomic():
        AIProviderOperation.objects.filter(pk=operation.pk).delete()

    operation.apply_state = AIProviderOperation.ApplyState.APPLIED
    operation.applied_at = timezone.now()
    operation.save(update_fields=['apply_state', 'applied_at', 'updated_at'])
    product.hard_delete()

    assert not Product.all_objects.filter(pk=product.pk).exists()
    assert AIProviderOperation.objects.filter(pk=operation.pk).exists()


@pytest.mark.django_db
def test_unresolved_web_research_owner_resists_direct_and_cascade_delete():
    from apps.products.models import Product
    from apps.web_research.models import WebResearchRun

    tenant = make_tenant('ai-provider-delete-web-run')
    product = Product.objects.create(
        tenant=tenant,
        article='AI-WEB-DELETE-1',
        name='Protected web paid result',
        price='1.00',
    )
    run = WebResearchRun.objects.create(tenant=tenant, product=product)
    operation = AIProviderOperation.objects.create(
        tenant=tenant,
        task_type=AITaskType.WEB_RESEARCH,
        provider='openai',
        model_id='test-model',
        reservation_key='ai-delete-web-run-reservation',
        reserved_amount=Decimal('1'),
        domain_type=AIProviderOperation.DomainType.WEB_RESEARCH_RUN,
        domain_reference=str(run.pk),
        status=AIProviderOperation.Status.PENDING_RECONCILIATION,
        apply_state=AIProviderOperation.ApplyState.NOT_REQUIRED,
    )

    with pytest.raises(ProtectedError, match='unresolved AI provider'), \
         transaction.atomic():
        run.delete()
    with pytest.raises(ProtectedError, match='unresolved AI provider'), \
         transaction.atomic():
        product.hard_delete()

    operation.status = AIProviderOperation.Status.RELEASED
    operation.released_at = timezone.now()
    operation.resolved_at = timezone.now()
    operation.save(update_fields=[
        'status', 'released_at', 'resolved_at', 'updated_at',
    ])
    product.hard_delete()

    assert not WebResearchRun.objects.filter(pk=run.pk).exists()
    operation.delete()
    assert not AIProviderOperation.objects.filter(pk=operation.pk).exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('domain_kind', ['product', 'web_run'])
def test_concurrent_ai_begin_and_owner_delete_cannot_create_orphan(
    monkeypatch,
    domain_kind,
):
    """Tenant->owner locking makes begin/delete outcomes serializable."""
    if connection.vendor != 'postgresql':
        pytest.skip('row-lock interleaving requires PostgreSQL')

    from apps.billing.ai_wallet import AIReservation
    from apps.products.models import Product
    from apps.web_research.models import WebResearchRun

    tenant = make_tenant(f'ai-owner-race-{domain_kind}')
    product = Product.objects.create(
        tenant=tenant,
        article=f'AI-RACE-{domain_kind}',
        name='AI owner race',
        price='1.00',
    )
    if domain_kind == 'product':
        owner = product
        domain_type = AIProviderOperation.DomainType.PRODUCT
    else:
        owner = WebResearchRun.objects.create(tenant=tenant, product=product)
        domain_type = AIProviderOperation.DomainType.WEB_RESEARCH_RUN

    begin_holds_owner_lock = threading.Event()
    delete_started = threading.Event()
    allow_begin_to_commit = threading.Event()
    begin_result: Queue = Queue()
    delete_result: Queue = Queue()

    def paused_reserve(_tenant, amount, **kwargs):
        begin_holds_owner_lock.set()
        assert allow_begin_to_commit.wait(timeout=10)
        return AIReservation(
            key=f'ai-owner-race:{domain_kind}',
            amount=Decimal(amount),
        )

    monkeypatch.setattr(AIWalletService, 'reserve', paused_reserve)

    def begin_worker():
        close_old_connections()
        try:
            operation = begin_ai_provider_operation(
                tenant=type(tenant).objects.get(pk=tenant.pk),
                task_type=(
                    AITaskType.DESCRIPTION
                    if domain_kind == 'product'
                    else AITaskType.WEB_RESEARCH
                ),
                provider='openai',
                model_id='race-model',
                reserved_amount=Decimal('1'),
                domain_type=domain_type,
                domain_reference=str(owner.pk),
            )
        except Exception as exc:  # pragma: no cover - asserted through queue
            begin_result.put(exc)
        else:
            begin_result.put(operation.pk)
        finally:
            close_old_connections()

    def delete_worker():
        close_old_connections()
        delete_started.set()
        try:
            if domain_kind == 'product':
                Product.all_objects.get(pk=owner.pk).hard_delete()
            else:
                WebResearchRun.objects.get(pk=owner.pk).delete()
        except Exception as exc:  # expected serialization result
            delete_result.put(exc)
        else:
            delete_result.put(None)
        finally:
            close_old_connections()

    begin_thread = threading.Thread(target=begin_worker, daemon=True)
    begin_thread.start()
    assert begin_holds_owner_lock.wait(timeout=10)
    delete_thread = threading.Thread(target=delete_worker, daemon=True)
    delete_thread.start()
    assert delete_started.wait(timeout=10)
    # The delete is now either waiting on Tenant or owner; let begin publish
    # the unresolved operation before the signal can complete its check.
    allow_begin_to_commit.set()
    begin_thread.join(timeout=10)
    delete_thread.join(timeout=10)
    assert not begin_thread.is_alive()
    assert not delete_thread.is_alive()

    operation_id = begin_result.get_nowait()
    assert not isinstance(operation_id, Exception)
    delete_error = delete_result.get_nowait()
    assert isinstance(delete_error, ProtectedError)
    assert AIProviderOperation.objects.filter(pk=operation_id).exists()
    if domain_kind == 'product':
        assert Product.all_objects.filter(pk=owner.pk).exists()
    else:
        assert WebResearchRun.objects.filter(pk=owner.pk).exists()


@pytest.mark.django_db
@pytest.mark.parametrize('domain_kind', ['product', 'web_run'])
def test_ai_begin_rejects_a_deleted_domain_owner_before_reserving(
    monkeypatch,
    domain_kind,
):
    from apps.products.models import Product
    from apps.web_research.models import WebResearchRun

    tenant = make_tenant(f'ai-missing-owner-{domain_kind}')
    product = Product.objects.create(
        tenant=tenant,
        article=f'AI-MISSING-OWNER-{domain_kind}',
        name='Deleted owner',
        price='1.00',
    )
    if domain_kind == 'product':
        owner_id = product.pk
        domain_type = AIProviderOperation.DomainType.PRODUCT
        task_type = AITaskType.DESCRIPTION
        product.hard_delete()
    else:
        run = WebResearchRun.objects.create(tenant=tenant, product=product)
        owner_id = run.pk
        domain_type = AIProviderOperation.DomainType.WEB_RESEARCH_RUN
        task_type = AITaskType.WEB_RESEARCH
        run.delete()

    def unexpected_reserve(*args, **kwargs):
        pytest.fail('wallet reservation crossed a missing-owner boundary')

    monkeypatch.setattr(AIWalletService, 'reserve', unexpected_reserve)

    with pytest.raises(ValueError, match='domain owner does not exist'):
        begin_ai_provider_operation(
            tenant=tenant,
            task_type=task_type,
            provider='openai',
            model_id='missing-owner-model',
            reserved_amount=Decimal('1'),
            domain_type=domain_type,
            domain_reference=str(owner_id),
        )
