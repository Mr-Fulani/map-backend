from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GATEWAY = (ROOT / 'scripts' / 'production_deploy_gateway.sh').read_text()
RELEASE = (ROOT / 'scripts' / 'production_release.sh').read_text()
DEPLOY_SCRIPT = (ROOT / 'deploy.sh').read_text()
INSTALLER = (ROOT / 'scripts' / 'install_production_host_services.sh').read_text()
MONITOR = (ROOT / '.github' / 'workflows' / 'production-monitor.yml').read_text()
DEPLOY = (ROOT / '.github' / 'workflows' / 'deploy.yml').read_text()
ROTATE_BACKUP_DB = (ROOT / 'scripts' / 'rotate_backup_db_password.sh').read_text()
BOOTSTRAP = (ROOT / 'scripts' / 'bootstrap_production_host_contract.sh').read_text()
BACKUP = (ROOT / 'scripts' / 'production_backup.sh').read_text()
BACKUP_CHECK = (ROOT / 'scripts' / 'production_backup_check.sh').read_text()
CHECKOUT_VALIDATOR = (
    ROOT / 'scripts' / 'validate_production_checkout.sh'
).read_text()
SUDOERS = (ROOT / 'ops' / 'sudoers' / 'saas-poster-deploy').read_text()
RELOAD_NGINX = (ROOT / 'scripts' / 'reload_production_nginx.sh').read_text()


def test_restricted_gateway_has_a_tiny_allowlist_and_never_evaluates_input():
    assert 'SSH_ORIGINAL_COMMAND' in GATEWAY
    assert 'deploy)' in GATEWAY
    assert 'backup-check)' in GATEWAY
    assert 'topology-check)' in GATEWAY
    assert 'capacity-check)' in GATEWAY
    assert 'eval ' not in GATEWAY
    assert 'bash -c' not in GATEWAY
    assert '^[0-9a-f]{40}$' in GATEWAY
    assert '/opt/saas_poster/scripts/' not in GATEWAY
    assert '/usr/local/sbin/saas-poster-verify-topology' in GATEWAY
    assert '/usr/local/sbin/saas-poster-check-capacity' in GATEWAY
    assert '/opt/saas_poster/scripts/' not in SUDOERS


def test_release_only_accepts_exact_current_main_descendant():
    assert 'ROOT_DIR="/opt/saas_poster"' in RELEASE
    assert '[[ "$EUID" -eq 0 ]]' in RELEASE
    assert 'git fetch --no-tags origin main' in RELEASE
    assert RELEASE.index('flock -n 9') < RELEASE.index('git fetch --no-tags origin main')
    assert 'export DEPLOY_LOCK_FD=9' in RELEASE
    assert '[[ "$TARGET_SHA" != "$MAIN_SHA" ]]' in RELEASE
    assert 'git merge-base --is-ancestor "$PREVIOUS_SHA" "$TARGET_SHA"' in RELEASE
    assert 'export PREVIOUS_SHA' in RELEASE
    assert RELEASE.index('saas-poster-validate-checkout') < RELEASE.index(
        'git fetch --no-tags origin main',
    )
    assert 'exec "$ROOT_DIR/deploy.sh" "$TARGET_SHA"' in RELEASE
    for unsafe in ('eval ', 'git pull', 'git reset', 'docker volume prune'):
        assert unsafe not in RELEASE


def test_release_restores_checkout_when_pre_runtime_validation_fails():
    assert 'restore_checkout_on_launcher_failure' in RELEASE
    assert RELEASE.index('trap restore_checkout_on_launcher_failure EXIT') < RELEASE.index(
        'git checkout --detach "$TARGET_SHA"',
    )
    assert RELEASE.index('checkout_changed=true') < RELEASE.index(
        'git checkout --detach "$TARGET_SHA"',
    )
    assert 'restore_checkout_before_runtime' in DEPLOY_SCRIPT
    assert DEPLOY_SCRIPT.index(
        "trap 'restore_checkout_before_runtime $?' ERR",
    ) < DEPLOY_SCRIPT.index('load_deploy_env "$DEPLOY_ENV_FILE"')
    assert DEPLOY_SCRIPT.index(
        "trap 'restore_checkout_before_runtime $?' ERR",
    ) < DEPLOY_SCRIPT.index('docker info >/dev/null')


def test_host_bootstrap_holds_release_lock_and_restores_actual_runtime_sha():
    assert '[[ "$EUID" -eq 0 ]]' in BOOTSTRAP
    assert BOOTSTRAP.index('flock -n 9') < BOOTSTRAP.index(
        'git fetch --no-tags origin main',
    )
    assert 'PREVIOUS_SHA="$(git rev-parse --verify' in BOOTSTRAP
    assert '[[ "$TARGET_SHA" == "$MAIN_SHA" ]]' in BOOTSTRAP
    assert 'git merge-base --is-ancestor "$PREVIOUS_SHA" "$TARGET_SHA"' in BOOTSTRAP
    assert BOOTSTRAP.index('checkout_changed=true') < BOOTSTRAP.index(
        'git checkout --detach "$TARGET_SHA"',
    )
    assert BOOTSTRAP.index('git checkout --detach "$TARGET_SHA"') < BOOTSTRAP.index(
        './scripts/install_production_host_services.sh',
    )
    assert BOOTSTRAP.index('./scripts/install_production_host_services.sh') < (
        BOOTSTRAP.rindex('git checkout --detach "$PREVIOUS_SHA"')
    )
    assert 'trap restore_checkout EXIT' in BOOTSTRAP
    assert BOOTSTRAP.index('flock -n 9') < BOOTSTRAP.index(
        'systemctl disable --now "$timer_unit"',
    )
    assert BOOTSTRAP.index('systemctl disable --now "$timer_unit"') < (
        BOOTSTRAP.index('git checkout --detach "$TARGET_SHA"')
    )
    assert 'systemctl is-active --quiet' in BOOTSTRAP
    assert '|| systemctl is-active --quiet saas-poster-backup-check.service' in BOOTSTRAP
    assert 'SAAS_POSTER_DEFER_TIMERS=true' in BOOTSTRAP
    assert 'HOST_CONTRACT_PENDING_FILE="$PROD_LOCK_DIR/host-contract-pending"' in BOOTSTRAP
    assert 'mv -f -- "$pending_tmp" "$HOST_CONTRACT_PENDING_FILE"' in BOOTSTRAP
    assert 'git reset' not in BOOTSTRAP
    assert BOOTSTRAP.index('validate_bootstrap_source') < BOOTSTRAP.index(
        'git fetch --no-tags origin main',
    )
    for installed_name in (
        'saas-poster-validate-checkout',
        'saas-poster-verify-topology',
        'saas-poster-check-capacity',
        'saas-poster-backup',
        'saas-poster-backup-check',
        'saas-poster-reload-nginx',
        'saas-poster-rotate-backup-db-password',
    ):
        assert f'/usr/local/sbin/{installed_name}' in BOOTSTRAP


def test_installer_creates_forced_key_root_units_and_certbot_hook():
    assert 'useradd --create-home --shell /bin/bash mapdeploy' in INSTALLER
    assert 'restrict,command=' in INSTALLER
    assert 'usermod --password "$random_password_hash"' in INSTALLER
    assert 'mapdeploy_groups="$(id -nG mapdeploy)"' in INSTALLER
    assert '"$mapdeploy_groups" == "mapdeploy"' in INSTALLER
    assert 'mapdeploy has unexpected supplementary or privileged groups' in INSTALLER
    assert 'sshd -t' in INSTALLER
    sshd = (ROOT / 'ops/ssh/90-saas-poster-mapdeploy.conf').read_text()
    assert 'AuthenticationMethods publickey' in sshd
    assert 'PasswordAuthentication no' in sshd
    assert 'ForceCommand /usr/local/sbin/saas-poster-deploy-gateway' in sshd
    assert 'visudo -cf' in INSTALLER
    assert 'systemctl enable --now saas-poster-backup.timer' in INSTALLER
    assert 'SAAS_POSTER_DEFER_TIMERS' in INSTALLER
    assert 'if [[ "$DEFER_TIMERS" == "true" ]]' in INSTALLER
    assert '/etc/letsencrypt/renewal-hooks/deploy' in INSTALLER
    assert INSTALLER.index('validate_install_source') < INSTALLER.index(
        'install -o root -g root -m 0755',
    )
    for installed_name in (
        'saas-poster-validate-checkout',
        'saas-poster-verify-topology',
        'saas-poster-check-capacity',
        'saas-poster-backup',
        'saas-poster-backup-check',
        'saas-poster-reload-nginx',
        'saas-poster-rotate-backup-db-password',
    ):
        assert f'/usr/local/sbin/{installed_name}' in INSTALLER
    assert 'ln -sfn /usr/local/sbin/saas-poster-reload-nginx' in INSTALLER


def test_privileged_host_entrypoints_reject_a_mutable_checkout():
    assert 'readlink -f -- "$ROOT_DIR"' in CHECKOUT_VALIDATOR
    assert "stat -c '%u'" in CHECKOUT_VALIDATOR
    assert '-perm /022' in CHECKOUT_VALIDATOR
    assert '! -user root' in CHECKOUT_VALIDATOR
    assert '-type l' in CHECKOUT_VALIDATOR
    for script in (RELEASE, BACKUP, BACKUP_CHECK, RELOAD_NGINX):
        assert 'saas-poster-validate-checkout' in script

    backup_unit = (
        ROOT / 'ops' / 'systemd' / 'saas-poster-backup.service'
    ).read_text()
    backup_check_unit = (
        ROOT / 'ops' / 'systemd' / 'saas-poster-backup-check.service'
    ).read_text()
    assert 'ExecStart=/usr/local/sbin/saas-poster-backup' in backup_unit
    assert 'ExecStart=/usr/local/sbin/saas-poster-backup-check' in (
        backup_check_unit
    )
    assert 'ExecStart=/opt/saas_poster/scripts/' not in backup_unit
    assert 'ExecStart=/opt/saas_poster/scripts/' not in backup_check_unit


def test_deploy_activates_backup_timers_only_after_successful_smoke():
    smoke = DEPLOY_SCRIPT.rindex('smoke_check')
    activate = DEPLOY_SCRIPT.index(
        'systemctl enable --now saas-poster-backup.timer',
        smoke,
    )
    marker_cleanup = DEPLOY_SCRIPT.index(
        'rm -f -- "$HOST_CONTRACT_PENDING_FILE"',
        activate,
    )
    trap_clear = DEPLOY_SCRIPT.index('trap - ERR HUP INT TERM', marker_cleanup)
    assert smoke < activate < marker_cleanup < trap_clear
    assert 'validate_pending_host_contract' in DEPLOY_SCRIPT


def test_systemd_backup_units_match_root_owned_production_checkout():
    for name in (
        'saas-poster-backup.service',
        'saas-poster-backup-check.service',
    ):
        unit = (ROOT / 'ops' / 'systemd' / name).read_text()
        assert 'User=root' in unit
        assert 'Group=root' in unit
        assert 'UMask=0077' in unit
        assert 'ProtectSystem=full' in unit
        assert 'NoNewPrivileges=true' in unit
        assert 'User=saas-poster' not in unit


def test_scheduled_backup_tools_share_release_lock_and_never_recreate_egress():
    for script in (BACKUP, BACKUP_CHECK):
        assert 'DEPLOY_LOCK_FILE="$PROD_LOCK_DIR/deploy.lock"' in script
        assert script.index('flock 9') < script.index('cd "$ROOT_DIR"')
        assert script.index('flock 9') < script.index('docker compose')
        assert "docker inspect --format '{{.State.Health.Status}}'" in script
        assert '--rm --no-deps' in script
        assert 'up -d' not in script


def test_scheduled_monitor_checks_public_backup_and_runtime_topology():
    assert "cron: '17 * * * *'" in MONITOR
    assert 'https://dodugir.com/api/v1/ready/' in MONITOR
    assert 'backup-check' in MONITOR
    assert 'topology-check' in MONITOR
    assert 'capacity-check' in MONITOR
    assert 'PROD_HOST_FINGERPRINT' in MONITOR
    assert 'Production monitor failed' in MONITOR
    assert 'test "$PROD_USER" = mapdeploy' in MONITOR
    assert 'test "$PROD_USER" = mapdeploy' in DEPLOY


def test_backup_db_rotation_uses_stdin_and_validates_a_read_only_role():
    assert 'DEPLOY_LOCK_FILE="$PROD_LOCK_DIR/deploy.lock"' in ROTATE_BACKUP_DB
    assert ROTATE_BACKUP_DB.index('flock 9') < ROTATE_BACKUP_DB.rindex(
        '\\password %s',
    )
    assert ROTATE_BACKUP_DB.index('flock -u 9') < ROTATE_BACKUP_DB.rindex(
        '/usr/local/sbin/saas-poster-backup',
    )
    assert 'secrets.token_urlsafe(48)' in ROTATE_BACKUP_DB
    assert "printf '\\\\password %s\\n' \"$backup_role\"" in ROTATE_BACKUP_DB
    assert "printf '%s\\n%s\\n' \"$backup_password\"" in ROTATE_BACKUP_DB
    assert '-e MAP_BACKUP_PASSWORD=' not in ROTATE_BACKUP_DB
    assert '-e MAP_BACKUP_ROLE=' not in ROTATE_BACKUP_DB
    assert 'MAP_BACKUP_PASSWORD=' not in ROTATE_BACKUP_DB
    assert '\\getenv map_backup_password' not in ROTATE_BACKUP_DB
    assert 'rolname <> current_user' in ROTATE_BACKUP_DB
    for unsafe_attribute in (
        'NOT rolsuper',
        'NOT rolcreaterole',
        'NOT rolcreatedb',
        'NOT rolreplication',
        'NOT rolbypassrls',
    ):
        assert unsafe_attribute in ROTATE_BACKUP_DB
    assert "granted_role.rolname = 'pg_read_all_data'" in ROTATE_BACKUP_DB
    assert "granted_role.rolname <> 'pg_read_all_data'" in ROTATE_BACKUP_DB
    assert "current_setting('password_encryption') = 'scram-sha-256'" in (
        ROTATE_BACKUP_DB
    )
    assert "has_table_privilege(target.rolname, relation.oid, 'INSERT')" in (
        ROTATE_BACKUP_DB
    )
    assert "has_schema_privilege(" in ROTATE_BACKUP_DB
    assert 'unsafe_default_acl' in ROTATE_BACKUP_DB
    assert 'mv -f -- "$publish_tmp" "$BACKUP_ENV_FILE"' in ROTATE_BACKUP_DB
    assert 'for target_path in (recovery_path, publish_path)' in ROTATE_BACKUP_DB
    assert ROTATE_BACKUP_DB.count('fsync_root_dir') >= 5
    assert "getattr(os, 'O_DIRECTORY', 0)" in ROTATE_BACKUP_DB
    assert ROTATE_BACKUP_DB.index('fsync_root_dir\n\nCOMPOSE=') < (
        ROTATE_BACKUP_DB.rindex('\\password %s')
    )
    assert ROTATE_BACKUP_DB.index('mv -f -- "$publish_tmp"') < ROTATE_BACKUP_DB.rindex(
        'fsync_root_dir',
    )
    assert '/usr/local/sbin/saas-poster-backup' in ROTATE_BACKUP_DB
    assert 'saas-poster-validate-checkout' in ROTATE_BACKUP_DB
    assert 'echo "$backup_password"' not in ROTATE_BACKUP_DB


def test_backup_db_rotation_persists_uncertain_state_before_password_boundary():
    marker_publish = ROTATE_BACKUP_DB.index(
        'mv -f -- "$marker_tmp" "$ROTATION_UNCERTAIN_FILE"',
    )
    password_boundary = ROTATE_BACKUP_DB.rindex('\\password %s')
    env_publish = ROTATE_BACKUP_DB.index(
        'mv -f -- "$publish_tmp" "$BACKUP_ENV_FILE"',
    )
    marker_clear = ROTATE_BACKUP_DB.rindex(
        'rm -f -- "$ROTATION_UNCERTAIN_FILE"',
    )

    assert 'ROTATION_UNCERTAIN_FILE="$ROOT_DIR/.backup-db-rotation-uncertain"' in (
        ROTATE_BACKUP_DB
    )
    assert 'fsync_file "$marker_tmp"' in ROTATE_BACKUP_DB
    assert marker_publish < password_boundary < env_publish < marker_clear
    assert ROTATE_BACKUP_DB.index(
        'password_change_may_have_happened=true',
    ) < password_boundary
    assert 'password_change_may_have_happened" == "true"' in ROTATE_BACKUP_DB
    assert 'rotation_resolved=true' in ROTATE_BACKUP_DB
    assert 'Recovery env preserved at: $env_tmp' in ROTATE_BACKUP_DB
    assert 'a previous password change may have committed' in ROTATE_BACKUP_DB
    assert "trap 'exit 129' HUP" in ROTATE_BACKUP_DB
    assert "trap 'exit 130' INT" in ROTATE_BACKUP_DB
    assert "trap 'exit 143' TERM" in ROTATE_BACKUP_DB
