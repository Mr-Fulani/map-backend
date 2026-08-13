from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = (ROOT / 'deploy.sh').read_text()
DEPLOY_WORKFLOW = (ROOT / '.github/workflows/deploy.yml').read_text()


def test_deploy_is_gated_by_ci_and_uses_exact_commit():
    assert 'workflow_run:' in DEPLOY_WORKFLOW
    assert "github.event.workflow_run.conclusion == 'success'" in DEPLOY_WORKFLOW
    assert "github.event.workflow_run.event == 'push'" in DEPLOY_WORKFLOW
    assert "github.event.workflow_run.head_branch == 'main'" in DEPLOY_WORKFLOW
    assert 'github.event.workflow_run.head_sha' in DEPLOY_WORKFLOW
    assert 'deploy "$DEPLOY_SHA"' in DEPLOY_WORKFLOW
    assert 'bash -se' not in DEPLOY_WORKFLOW


def test_previous_sha_is_explicit_and_validated_before_docker_mutation():
    assert 'PREVIOUS_SHA="${PREVIOUS_SHA:-}"' in DEPLOY_SCRIPT
    assert 'PREVIOUS_SHA:-$(git rev-parse HEAD' not in DEPLOY_SCRIPT
    validation = DEPLOY_SCRIPT.index(
        'PREVIOUS_SHA обязателен и должен быть сохранён до checkout TARGET_SHA.'
    )
    first_build = DEPLOY_SCRIPT.index('build --pull')

    assert validation < first_build
    assert 'git cat-file -e "${PREVIOUS_SHA}^{commit}"' in DEPLOY_SCRIPT
    assert 'git merge-base --is-ancestor "$PREVIOUS_SHA" "$TARGET_SHA"' in (
        DEPLOY_SCRIPT
    )
    assert DEPLOY_SCRIPT.index('saas-poster-validate-checkout') < first_build
    assert '[[ "$ROOT_DIR" == "/opt/saas_poster" ]]' in DEPLOY_SCRIPT


def test_image_build_rejects_tracked_and_untracked_worktree_drift():
    status_command = (
        'git status --porcelain=v1 --untracked-files=normal '
        '--ignore-submodules=none'
    )

    assert status_command in DEPLOY_SCRIPT
    assert DEPLOY_SCRIPT.index(status_command) < DEPLOY_SCRIPT.index('build --pull')
    assert 'git diff --quiet' not in DEPLOY_SCRIPT
    assert 'git diff --cached --quiet' not in DEPLOY_SCRIPT


def test_stale_installed_host_entrypoints_fail_before_docker_mutation():
    first_build = DEPLOY_SCRIPT.index('build --pull')
    assert (
        'scripts/production_release.sh:'
        '/usr/local/sbin/saas-poster-release:755'
    ) in DEPLOY_SCRIPT
    assert (
        'scripts/production_deploy_gateway.sh:'
        '/usr/local/sbin/saas-poster-deploy-gateway:755'
    ) in DEPLOY_SCRIPT
    for installed_contract in (
        'scripts/validate_production_checkout.sh:'
        '/usr/local/sbin/saas-poster-validate-checkout:755',
        'scripts/verify_production_topology.sh:'
        '/usr/local/sbin/saas-poster-verify-topology:755',
        'scripts/check_production_capacity.sh:'
        '/usr/local/sbin/saas-poster-check-capacity:755',
        'scripts/production_backup.sh:'
        '/usr/local/sbin/saas-poster-backup:755',
        'scripts/production_backup_check.sh:'
        '/usr/local/sbin/saas-poster-backup-check:755',
        'scripts/reload_production_nginx.sh:'
        '/usr/local/sbin/saas-poster-reload-nginx:755',
        'scripts/rotate_backup_db_password.sh:'
        '/usr/local/sbin/saas-poster-rotate-backup-db-password:755',
    ):
        assert installed_contract in DEPLOY_SCRIPT
    assert '== "/usr/local/sbin/saas-poster-reload-nginx"' in DEPLOY_SCRIPT
    assert 'ops/sudoers/saas-poster-deploy:/etc/sudoers.d/' in DEPLOY_SCRIPT
    assert 'ops/systemd/saas-poster-backup.service:/etc/systemd/system/' in DEPLOY_SCRIPT
    verification = DEPLOY_SCRIPT.rindex('    verify_installed_host_contract\n')
    assert verification < first_build


def test_deploy_env_is_validated_then_allowlist_parsed_without_evaluation():
    allowed_keys = (
        'PROD_SMOKE_URL',
        'PROD_MIN_FREE_DISK_MB',
        'PROD_HEALTH_RETRIES',
        'PROD_HEALTH_INTERVAL_SECONDS',
        'PROD_LOG_TAIL',
        'PROD_ROLLBACK_ENABLED',
        'PROD_BACKUP_TIMEOUT_SECONDS',
        'PROD_DRAIN_TIMEOUT_SECONDS',
        'PROD_BROKER_MIGRATION_CONFIRMED',
    )

    early_validation = DEPLOY_SCRIPT.index(
        'validate_private_regular_file "$DEPLOY_ENV_FILE"'
    )
    parser_call = DEPLOY_SCRIPT.index('load_deploy_env "$DEPLOY_ENV_FILE"')
    assert early_validation < parser_call
    assert 'source "$DEPLOY_ENV_FILE"' not in DEPLOY_SCRIPT
    assert 'eval ' not in DEPLOY_SCRIPT
    assert 'local -A seen_keys=()' in DEPLOY_SCRIPT
    assert 'параметр ${key} указан повторно' in DEPLOY_SCRIPT
    assert 'shell-подстановки в значениях запрещены' in DEPLOY_SCRIPT
    for key in allowed_keys:
        assert key in DEPLOY_SCRIPT


def test_operator_env_files_must_be_private_regular_files_owned_by_deploy_user():
    assert '[[ -f "$secret_file" && ! -L "$secret_file" && -O "$secret_file" ]]' in DEPLOY_SCRIPT
    assert '"$secret_mode" == "600" || "$secret_mode" == "400"' in DEPLOY_SCRIPT
    assert 'validate_private_regular_file "$ROOT_DIR/.env"' in DEPLOY_SCRIPT
    assert 'validate_private_regular_file "$ROOT_DIR/.backup.env"' in DEPLOY_SCRIPT
    assert 'validate_private_regular_file "$DEPLOY_ENV_FILE"' in DEPLOY_SCRIPT


def test_deploy_never_performs_host_wide_cleanup_or_branch_pull():
    forbidden = (
        'git pull',
        'docker container prune',
        'docker image prune',
        'docker builder prune',
        'docker system prune',
        'docker volume prune',
    )
    for command in forbidden:
        assert command not in DEPLOY_SCRIPT


def test_deploy_requires_realistic_build_disk_headroom():
    assert 'PROD_MIN_FREE_DISK_MB="${PROD_MIN_FREE_DISK_MB:-16384}"' in DEPLOY_SCRIPT
    assert '(( PROD_MIN_FREE_DISK_MB >= 8192 ))' in DEPLOY_SCRIPT
    capacity = DEPLOY_SCRIPT.index('"$ROOT_DIR/scripts/check_production_capacity.sh"')
    build = DEPLOY_SCRIPT.index('build --pull')
    assert capacity < build


def test_migration_precedes_application_rollout_and_smoke_check():
    execution = DEPLOY_SCRIPT.split('echo "==> Preflight для commit', 1)[1]
    build = execution.index('build --pull')
    migration = execution.index('migrate --noinput')
    rollout = execution.index('up -d --no-build --remove-orphans')
    smoke = execution.index('\nsmoke_check\n')

    assert build < migration < rollout < smoke


def test_egress_proxy_is_built_early_but_recreated_only_after_writer_drain():
    execution = DEPLOY_SCRIPT.split('echo "==> Preflight для commit', 1)[1]
    build = execution.index('build --pull egress_proxy')
    drain = execution.index('drain_application_writers\n')
    recreate = execution.index(
        'up -d --no-build db redis redis_broker egress_proxy',
    )
    backup = execution.index('DEPLOY_PHASE="pre-migration database backup"')
    assert build < drain < recreate < backup
    assert 'existing != ${#infrastructure[@]}' in DEPLOY_SCRIPT
    assert 'автоматическое recreate до drain запрещено' in DEPLOY_SCRIPT


def test_target_egress_is_revalidated_after_recreate_before_migration():
    execution = DEPLOY_SCRIPT.split('echo "==> Preflight для commit', 1)[1]
    recreate = execution.index(
        'up -d --no-build db redis redis_broker egress_proxy',
    )
    migration_started = execution.index('MIGRATIONS_STARTED=true', recreate)
    after_recreate = execution[recreate:migration_started]

    assert after_recreate.count(
        'python manage.py check_redis_connectivity --require-target-limits',
    ) == 1
    assert after_recreate.count('python manage.py check_email_connectivity') == 1
    assert after_recreate.count(
        'python manage.py check_public_http_connectivity',
    ) == 1
    assert after_recreate.index(
        'check_redis_connectivity --require-target-limits',
    ) < after_recreate.index('DEPLOY_PHASE="pre-migration database backup"')
    assert after_recreate.index('check_email_connectivity') < after_recreate.index(
        'DEPLOY_PHASE="pre-migration database backup"',
    )
    assert after_recreate.index('check_public_http_connectivity') < after_recreate.index(
        'DEPLOY_PHASE="pre-migration database backup"',
    )


def test_idempotent_plan_seed_runs_after_migration_before_rollout():
    execution = DEPLOY_SCRIPT.split('echo "==> Preflight для commit', 1)[1]
    migration = execution.index('migrate --noinput')
    seed = execution.index('python manage.py seed_plans')
    rollout = execution.index('up -d --no-build --remove-orphans')

    assert migration < seed < rollout


def test_old_application_writers_are_drained_before_backup_and_migration():
    execution = DEPLOY_SCRIPT
    services_changed = execution.rindex('SERVICES_CHANGED=true\n')
    drain = execution.index('drain_application_writers\n')
    backup = execution.index('DEPLOY_PHASE="pre-migration database backup"')
    migration_started = execution.index('MIGRATIONS_STARTED=true', drain)
    migration = execution.index('migrate --noinput', migration_started)

    assert services_changed < drain < backup < migration_started < migration
    assert 'stop -t 30 nginx' in execution
    assert 'stop -t "$PROD_DRAIN_TIMEOUT_SECONDS" "${DRAIN_SERVICES[@]}"' in execution


def test_drain_failure_or_signal_restores_previous_runtime():
    execution = DEPLOY_SCRIPT
    services_changed = execution.rindex('SERVICES_CHANGED=true\n')
    drain = execution.index('drain_application_writers\n', services_changed)
    rollback_guard = execution.index(
        'elif [[ "$SERVICES_CHANGED" == "true"',
    )

    assert services_changed < drain
    assert rollback_guard < services_changed
    for signal, exit_code in (
        ('ERR', '$?'), ('HUP', '129'), ('INT', '130'), ('TERM', '143'),
    ):
        assert f"trap 'rollback_deployment {exit_code}' {signal}" in execution
    assert '"${ROLLBACK_APPLICATION_SERVICES[@]}"' in execution[rollback_guard:]


def test_runtime_redis_connectivity_is_checked_before_the_no_return_point():
    execution = DEPLOY_SCRIPT
    connectivity = execution.index('python manage.py check_redis_connectivity')
    drain = execution.index('drain_application_writers\n')
    migration_started = execution.index('MIGRATIONS_STARTED=true', drain)

    assert connectivity < drain < migration_started
    assert 'timeout --foreground --signal=TERM --kill-after=5s 60s' in execution
    assert 'run --rm --no-deps django' in execution


def test_authenticated_smtp_connectivity_is_checked_before_drain():
    execution = DEPLOY_SCRIPT
    redis_connectivity = execution.index('python manage.py check_redis_connectivity')
    email_connectivity = execution.index('python manage.py check_email_connectivity')
    drain = execution.index('drain_application_writers\n')
    migration_started = execution.index('MIGRATIONS_STARTED=true', drain)

    assert redis_connectivity < email_connectivity < drain < migration_started
    email_preflight = execution[redis_connectivity:drain]
    assert 'timeout --foreground --signal=TERM --kill-after=5s 60s' in email_preflight
    assert 'run --rm --no-deps django' in email_preflight


def test_public_https_transport_is_checked_before_drain():
    execution = DEPLOY_SCRIPT
    email_connectivity = execution.index('python manage.py check_email_connectivity')
    public_connectivity = execution.index(
        'python manage.py check_public_http_connectivity'
    )
    drain = execution.index('drain_application_writers\n')
    migration_started = execution.index('MIGRATIONS_STARTED=true', drain)

    assert email_connectivity < public_connectivity < drain < migration_started
    public_preflight = execution[email_connectivity:drain]
    assert 'timeout --foreground --signal=TERM --kill-after=5s 60s' in public_preflight
    assert 'run --rm --no-deps django' in public_preflight


def test_rollback_never_restarts_old_writers_after_migration_started():
    execution = DEPLOY_SCRIPT
    guard = execution.index('if [[ "$MIGRATIONS_STARTED" == "true" ]]')
    old_release = execution.index('Возврат application-сервисов')

    assert guard < old_release
    assert 'старый application release автоматически не запускается' in execution
    migration_failure = execution[guard:old_release]
    assert 'stop -t 30 nginx' in migration_failure
    assert '"${DRAIN_SERVICES[@]}" || true' in migration_failure


def test_runtime_topology_is_verified_after_service_readiness_before_smoke():
    execution = DEPLOY_SCRIPT
    readiness = execution.index('for service in "${HEALTH_SERVICES[@]}"')
    topology = execution.index('scripts/verify_production_topology.sh', readiness)
    smoke = execution.index('\nsmoke_check\n', topology)

    assert readiness < topology < smoke


def test_deploy_has_readiness_lock_and_controlled_rollback():
    assert 'PROD_LOCK_DIR="/run/lock/saas-poster"' in DEPLOY_SCRIPT
    assert 'DEPLOY_LOCK_FILE="$PROD_LOCK_DIR/deploy.lock"' in DEPLOY_SCRIPT
    assert '-O "$PROD_LOCK_DIR"' in DEPLOY_SCRIPT
    assert 'должен иметь права 700' in DEPLOY_SCRIPT
    assert 'flock -n 9' in DEPLOY_SCRIPT
    assert DEPLOY_SCRIPT.index('flock -n 9') < DEPLOY_SCRIPT.index(
        'load_deploy_env "$DEPLOY_ENV_FILE"',
    )
    assert 'DEPLOY_LOCK_FD' in DEPLOY_SCRIPT
    assert 'wait_for_service' in DEPLOY_SCRIPT
    assert 'docker image tag' in DEPLOY_SCRIPT
    assert '"${ROLLBACK_INFRASTRUCTURE_SERVICES[@]}"' in DEPLOY_SCRIPT
    assert '"${ROLLBACK_APPLICATION_SERVICES[@]}"' in DEPLOY_SCRIPT
    assert 'ROLLBACK_SERVICES=(egress_proxy "${BUILD_SERVICES[@]}")' in DEPLOY_SCRIPT
    assert 'ROLLBACK_INFRASTRUCTURE_SERVICES=(db redis redis_broker egress_proxy)' in DEPLOY_SCRIPT
    assert 'ROLLBACK_APPLICATION_SERVICES=(django celery_worker ' in DEPLOY_SCRIPT
    rollback_checkout = DEPLOY_SCRIPT.index('git checkout --detach "$PREVIOUS_SHA"')
    infra_restore = DEPLOY_SCRIPT.index(
        '"${ROLLBACK_INFRASTRUCTURE_SERVICES[@]}"', rollback_checkout,
    )
    redis_probe = DEPLOY_SCRIPT.index('probe_redis_writes_after_rollback', infra_restore)
    app_restore = DEPLOY_SCRIPT.index(
        '"${ROLLBACK_APPLICATION_SERVICES[@]}"', redis_probe,
    )
    assert rollback_checkout < infra_restore < redis_probe < app_restore
    assert 'MIGRATIONS_APPLIED' in DEPLOY_SCRIPT


def test_patched_egress_is_built_before_runtime_is_changed():
    build_proxy = DEPLOY_SCRIPT.index('build --pull egress_proxy')
    services_changed = DEPLOY_SCRIPT.index('SERVICES_CHANGED=true', build_proxy)
    start_infrastructure = DEPLOY_SCRIPT.index(
        'up -d --no-build db redis redis_broker egress_proxy',
    )

    assert build_proxy < services_changed < start_infrastructure


def test_deploy_pins_the_production_compose_scope():
    assert DEPLOY_SCRIPT.count('docker compose') == 1
    assert '--project-name saas_poster' in DEPLOY_SCRIPT
    assert '--project-directory "$ROOT_DIR"' in DEPLOY_SCRIPT
    assert '-f "$COMPOSE_FILE"' in DEPLOY_SCRIPT
