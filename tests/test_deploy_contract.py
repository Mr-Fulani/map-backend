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
    assert 'PREVIOUS_SHA="$previous_sha" ./deploy.sh "$target_sha"' in DEPLOY_WORKFLOW


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


def test_migration_precedes_application_rollout_and_smoke_check():
    execution = DEPLOY_SCRIPT.split('echo "==> Preflight для commit', 1)[1]
    build = execution.index('build --pull')
    migration = execution.index('migrate --noinput')
    rollout = execution.index('up -d --no-build --remove-orphans')
    smoke = execution.index('\nsmoke_check\n')

    assert build < migration < rollout < smoke


def test_deploy_has_readiness_lock_and_controlled_rollback():
    assert 'flock -n 9' in DEPLOY_SCRIPT
    assert 'wait_for_service' in DEPLOY_SCRIPT
    assert 'docker image tag' in DEPLOY_SCRIPT
    assert '--force-recreate "${APPLICATION_SERVICES[@]}"' in DEPLOY_SCRIPT
    assert 'MIGRATIONS_APPLIED' in DEPLOY_SCRIPT
