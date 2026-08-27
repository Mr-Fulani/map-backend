import ast
import json
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
DEV_COMPOSE = yaml.safe_load((ROOT / 'docker-compose.yml').read_text())
COMPOSE = yaml.safe_load((ROOT / 'docker-compose.prod.yml').read_text())
RESTORE_COMPOSE = yaml.safe_load((ROOT / 'docker-compose.restore.yml').read_text())
CI_RUNTIME_COMPOSE = yaml.safe_load(
    (ROOT / 'docker-compose.ci-runtime.yml').read_text()
)
CI_WORKFLOW = (ROOT / '.github/workflows/ci.yml').read_text()
DEPLOY_WORKFLOW = (ROOT / '.github/workflows/deploy.yml').read_text()
NGINX_CONFIG = (ROOT / 'nginx.conf').read_text()
SQUID_CONFIG = (ROOT / 'egress-proxy.conf').read_text()
DEPENDABOT = yaml.safe_load((ROOT / '.github/dependabot.yml').read_text())
NPM_TARBALL_SHA512 = (
    'b885e890b9418fa1693544d05f53e64f9a73ec194837d4258b15fecdd692347b'
    '1dd2a517b1b0cbaf9d31cd8e92c3b70956bd2ecc72833a57b4b3098f5bfa7943'
)
TRIVY_TARBALL_SHA256 = (
    'bbb64b9695866ce4a7a8f5c9592002c5961cab378577fa3f8a040df362b9b2ea'
)
BACKUP_LIFECYCLE = json.loads(
    (ROOT / 'ops/s3/backup-lifecycle.json').read_text()
)
TRIVY_EXCEPTIONS = yaml.safe_load((ROOT / '.trivyignore.yaml').read_text())
EXPECTED_GOSU_EXCEPTIONS = {
    'CVE-2025-68121',
    'CVE-2025-61726',
    'CVE-2025-61729',
    'CVE-2026-25679',
    'CVE-2026-27145',
    'CVE-2026-32280',
    'CVE-2026-32281',
    'CVE-2026-32283',
    'CVE-2026-33811',
    'CVE-2026-33814',
    'CVE-2026-33818',
    'CVE-2026-39820',
    'CVE-2026-39821',
    'CVE-2026-39822',
    'CVE-2026-39836',
    'CVE-2026-42499',
    'CVE-2026-42504',
    'CVE-2026-56853',
    'CVE-2026-56858',
    'CVE-2026-56859',
    'CVE-2026-56860',
    'CVE-2026-56862',
}


def test_every_long_running_service_has_restart_and_log_rotation():
    services = COMPOSE['services']
    assert services

    for name, service in services.items():
        if 'ops' in service.get('profiles', []):
            assert service.get('restart') == 'no', name
        else:
            assert service.get('restart') == 'unless-stopped', name
        logging = service.get('logging', {})
        assert logging.get('driver') == 'json-file', name
        assert logging.get('options', {}).get('max-size') == '10m', name
        assert logging.get('options', {}).get('max-file') == '5', name
        assert service.get('stop_grace_period'), name


def test_stateless_application_services_run_with_least_privilege():
    services = COMPOSE['services']

    for name in (
        'django',
        'celery_worker',
        'celery_beat',
        'celery_worker_images',
        'frontend',
        'backup',
    ):
        service = services[name]
        assert service['read_only'] is True, name
        assert service['cap_drop'] == ['ALL'], name
        assert service['security_opt'] == ['no-new-privileges:true'], name
        assert service.get('user') not in {None, '0', '0:0', 'root'}, name
        assert service['environment']['HOME'] == '/tmp', name
        assert service['environment']['XDG_CACHE_HOME'] == '/tmp/.cache', name

    for name in (
        'django',
        'celery_worker',
        'celery_beat',
        'celery_worker_images',
        'frontend',
    ):
        assert COMPOSE['services'][name].get('tmpfs'), name

    assert '/tmp' in services['backup']['volumes']


def test_every_production_service_blocks_privilege_escalation():
    for name, service in COMPOSE['services'].items():
        assert service['security_opt'] == ['no-new-privileges:true'], name


def test_runtime_healthchecks_cover_http_workers_beat_and_proxy():
    services = COMPOSE['services']
    proxy_healthcheck = [
        'CMD', '/bin/bash', '-ec', 'exec 3<>/dev/tcp/127.0.0.1/3128',
    ]

    assert services['django']['healthcheck']['test'] == [
        'CMD', 'python', '-m', 'apps.core.healthchecks', 'django-liveness',
    ]
    assert 'worker-main@' in services['celery_worker']['command']
    assert 'worker-main@' in ' '.join(services['celery_worker']['healthcheck']['test'])
    assert 'worker-images@' in ' '.join(
        services['celery_worker_images']['healthcheck']['test']
    )
    assert 'HeartbeatDatabaseScheduler' in services['celery_beat']['command']
    assert services['celery_beat']['healthcheck']['test'][-1] == 'celery-beat'
    assert services['egress_proxy']['healthcheck']['test'] == proxy_healthcheck
    assert (
        RESTORE_COMPOSE['services']['egress_proxy']['healthcheck']['test']
        == proxy_healthcheck
    )
    assert '/nginx-health' in ' '.join(services['nginx']['healthcheck']['test'])


def test_celery_beat_receives_sigterm_through_init():
    beat = COMPOSE['services']['celery_beat']

    assert beat['init'] is True
    assert beat['stop_signal'] == 'SIGTERM'
    assert beat['stop_grace_period'] == '45s'
    assert not beat['command'].startswith(('sh ', 'bash '))


def test_every_declared_celery_queue_has_a_production_consumer():
    base_settings = (ROOT / 'config' / 'settings' / 'base.py').read_text()
    queue_block = re.search(
        r'CELERY_TASK_QUEUES = (?P<queues>\{.*?\n\})\nCELERY_TASK_DEFAULT_QUEUE',
        base_settings,
        re.DOTALL,
    )
    assert queue_block
    declared = set(ast.literal_eval(queue_block['queues']))

    consumed = set()
    for service_name in ('celery_worker', 'celery_worker_images'):
        command = COMPOSE['services'][service_name]['command']
        match = re.search(r'(?:^|\s)-Q\s+([^\s]+)', command)
        assert match, service_name
        consumed.update(match.group(1).split(','))

    assert consumed == declared
    settings_tree = ast.parse(base_settings)
    assignments = {
        target.id: ast.literal_eval(node.value)
        for node in settings_tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
        and target.id in {
            'CELERY_TASK_CREATE_MISSING_QUEUES',
            'CELERY_TASK_PROTOCOL',
        }
    }
    assert assignments['CELERY_TASK_CREATE_MISSING_QUEUES'] is False
    assert assignments['CELERY_TASK_PROTOCOL'] == 2


def test_literal_task_and_periodic_routes_use_only_declared_queues():
    base_settings = (ROOT / 'config' / 'settings' / 'base.py').read_text()
    queue_block = re.search(
        r'CELERY_TASK_QUEUES = (?P<queues>\{.*?\n\})\nCELERY_TASK_DEFAULT_QUEUE',
        base_settings,
        re.DOTALL,
    )
    assert queue_block
    declared = set(ast.literal_eval(queue_block['queues']))

    routed = set()
    for path in (ROOT / 'apps').glob('*/tasks.py'):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                for keyword in decorator.keywords:
                    if (
                        keyword.arg == 'queue'
                        and isinstance(keyword.value, ast.Constant)
                        and isinstance(keyword.value.value, str)
                    ):
                        routed.add(keyword.value.value)

    periodic_tree = ast.parse(
        (ROOT / 'apps/core/management/commands/setup_periodic_tasks.py').read_text()
    )
    for node in ast.walk(periodic_tree):
        if not isinstance(node, ast.Dict):
            continue
        values = {
            key.value: value.value
            for key, value in zip(node.keys, node.values)
            if isinstance(key, ast.Constant)
            and isinstance(key.value, str)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        }
        if 'task' in values and 'queue' in values:
            routed.add(values['queue'])

    dispatch_tree = ast.parse((ROOT / 'apps/core/dispatch.py').read_text())
    for node in ast.walk(dispatch_tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == 'DURABLE_TASK_QUEUES'
            for target in node.targets
        ):
            continue
        routed.update(ast.literal_eval(node.value).values())

    assert routed
    assert routed <= declared


def test_public_ingress_is_isolated_to_nginx():
    services = COMPOSE['services']

    assert COMPOSE['networks']['backend']['internal'] is True
    assert COMPOSE['networks']['ingress_public'] is None
    assert set(services['nginx']['networks']) == {'backend', 'ingress_public'}
    assert services['nginx']['ports'] == ['80:80', '443:443']
    for name, service in services.items():
        if name != 'nginx':
            assert 'ingress_public' not in service.get('networks', []), name


def test_ci_runtime_override_uses_ephemeral_certs_and_private_dns_target():
    nginx_volumes = CI_RUNTIME_COMPOSE['services']['nginx']['volumes']

    assert all('/etc/letsencrypt/' in volume for volume in nginx_volumes)
    assert all('CI_CERTIFICATE_' in volume for volume in nginx_volumes)
    assert CI_RUNTIME_COMPOSE['services']['egress_proxy']['extra_hosts'] == [
        'ci-private-target.dodugir.com:127.0.0.1',
    ]


def test_cache_and_durable_broker_are_separate_services():
    services = COMPOSE['services']
    cache_command = ' '.join(services['redis']['command'])
    broker_command = ' '.join(services['redis_broker']['command'])

    assert 'allkeys-lru' in cache_command
    assert 'appendonly yes' in broker_command
    assert 'appendfsync everysec' in broker_command
    assert 'noeviction' in broker_command
    assert services['redis_broker']['volumes'] == ['redis_broker_data:/data']
    for name in ('django', 'celery_worker', 'celery_beat', 'celery_worker_images'):
        assert services[name]['depends_on']['redis_broker']['condition'] == 'service_healthy'


def test_backup_is_one_shot_isolated_and_persistent_only_for_its_lock():
    backup = COMPOSE['services']['backup']

    assert backup['profiles'] == ['ops']
    assert backup['restart'] == 'no'
    assert backup['build']['dockerfile'] == 'backup/Dockerfile'
    assert backup['env_file'] == ['.backup.env']
    assert backup['volumes'] == ['backup_state:/state', '/tmp']
    assert backup['networks'] == ['backend']
    assert backup['environment']['HTTP_PROXY'] == 'http://egress_proxy:3128'
    assert backup['depends_on']['egress_proxy']['condition'] == 'service_healthy'
    assert 'ports' not in backup
    assert COMPOSE['volumes']['backup_state'] is None


def test_external_container_images_are_pinned_by_manifest_digest():
    image_pattern = re.compile(r'^[^@\s]+@sha256:[0-9a-f]{64}$')

    for compose in (DEV_COMPOSE, COMPOSE, RESTORE_COMPOSE):
        for name, service in compose['services'].items():
            if 'image' not in service or 'build' in service:
                continue
            assert image_pattern.fullmatch(service['image']), name

    ci_config = yaml.safe_load(CI_WORKFLOW)
    for job_name in ('backend-quality', 'backend-tests'):
        for name, service in ci_config['jobs'][job_name]['services'].items():
            assert image_pattern.fullmatch(service['image']), (job_name, name)


def test_backend_shards_prepare_backup_source_database_before_pytest():
    backend_tests = yaml.safe_load(CI_WORKFLOW)['jobs']['backend-tests']
    steps = backend_tests['steps']
    step_names = [step['name'] for step in steps]
    migration_name = 'Подготовить source database для backup integration'
    pytest_name = 'Запустить свою полную часть backend-тестов'

    assert backend_tests['env']['BACKUP_INTEGRATION_DATABASE_URL'].endswith(
        '/map_db'
    )
    assert step_names.index(migration_name) < step_names.index(pytest_name)
    migration_step = next(step for step in steps if step['name'] == migration_name)
    assert migration_step['run'] == 'python manage.py migrate --noinput'


def test_backend_coverage_artifacts_survive_failed_job_reruns():
    jobs = yaml.safe_load(CI_WORKFLOW)['jobs']
    upload_step = next(
        step
        for step in jobs['backend-tests']['steps']
        if step['name'] == 'Передать coverage своей части'
    )
    download_step = next(
        step
        for step in jobs['coverage']['steps']
        if step['name'] == 'Получить coverage всех частей'
    )

    assert upload_step['with']['name'] == (
        'coverage-${{ matrix.shard }}-${{ github.run_id }}'
    )
    assert upload_step['with']['overwrite'] is True
    assert download_step['with']['pattern'] == (
        'coverage-*-${{ github.run_id }}'
    )


def test_dockerfiles_pin_base_images_and_production_runs_non_root():
    external_image = re.compile(r'^[^\s@]+@sha256:[0-9a-f]{64}$')
    dockerfiles = {
        'backend': (ROOT / 'Dockerfile').read_text(),
        'frontend': (ROOT / 'frontend/Dockerfile').read_text(),
        'backup': (ROOT / 'backup/Dockerfile').read_text(),
        'nginx': (ROOT / 'ops/nginx/Dockerfile').read_text(),
        'postgres': (ROOT / 'ops/postgres/Dockerfile').read_text(),
        'egress_proxy': (ROOT / 'egress-proxy/Dockerfile').read_text(),
    }

    for name, dockerfile in dockerfiles.items():
        from_lines = re.findall(r'^FROM\s+.+$', dockerfile, re.MULTILINE)
        assert from_lines, name
        known_stages = set()
        for line in from_lines:
            match = re.fullmatch(
                r'FROM\s+(\S+)(?:\s+AS\s+(\w+))?',
                line,
                re.IGNORECASE,
            )
            assert match, (name, line)
            source, stage = match.groups()
            assert source in known_stages or external_image.fullmatch(source), (
                name, line,
            )
            if stage:
                known_stages.add(stage)

    assert dockerfiles['backend'].splitlines()[0] == (
        'FROM python:3.12.13-slim@sha256:'
        '229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36'
    )
    assert '--only-upgrade' in dockerfiles['backend']
    for package in (
        'bsdutils',
        'libblkid1',
        'liblastlog2-2',
        'libmount1',
        'libsmartcols1',
        'libssl3t64',
        'libuuid1',
        'login',
        'mount',
        'openssl',
        'openssl-provider-legacy',
        'util-linux',
    ):
        assert package in dockerfiles['backend']
    assert '2.41.5-0+deb13u1' in dockerfiles['backend']
    assert '1:2.41.5-0+deb13u1' in dockerfiles['backend']
    assert '1:4.16.0-2+really2.41.5-0+deb13u1' in dockerfiles['backend']
    assert 'for package in libssl3t64 openssl openssl-provider-legacy' in (
        dockerfiles['backend']
    )
    assert 'ge 3.5.7-1~deb13u2' in dockerfiles['backend']
    assert dockerfiles['frontend'].splitlines()[0] == (
        'FROM node:24.18.0-alpine@sha256:'
        'a0b9bf06e4e6193cf7a0f58816cc935ff8c2a908f81e6f1a95432d679c54fbfd '
        'AS base'
    )
    assert dockerfiles['backup'].splitlines()[0] == (
        'FROM postgres:16.14-alpine@sha256:'
        '57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777'
    )
    assert dockerfiles['postgres'].splitlines()[0] == (
        'FROM postgres:16.14-alpine@sha256:'
        '57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777'
    )
    assert dockerfiles['nginx'].splitlines()[0] == (
        'FROM nginx:1.30.4-alpine@sha256:'
        '97d490c12ba55b4946b01546d1c3ed324e8d41ab1c9fcb2a616aa470620e5b46'
    )
    for name in ('frontend', 'backup', 'nginx', 'postgres'):
        assert 'apk add --no-cache --upgrade' in dockerfiles[name], name
        assert 'libcrypto3' in dockerfiles[name], name
        assert 'libssl3' in dockerfiles[name], name
        assert 'apk version -t "$version" 3.5.8-r0' in dockerfiles[name], name
    assert dockerfiles['egress_proxy'].splitlines()[0] == (
        'FROM ubuntu/squid:6.6-24.04_edge@sha256:'
        '8a3baed477e2c282ab8aa5edad442f69873246964f225c5c2ae8364b6610963c'
    )
    assert '--only-upgrade' in dockerfiles['egress_proxy']
    assert 'for package in libssl3t64 openssl' in dockerfiles['egress_proxy']
    assert 'dpkg --compare-versions' in dockerfiles['egress_proxy']
    assert 'ge 3.0.13-0ubuntu3.11' in dockerfiles['egress_proxy']
    assert '\nUSER app\n' in dockerfiles['backend']
    assert '/usr/sbin/nologin' in dockerfiles['backend']
    frontend_runner = dockerfiles['frontend'].split(' AS runner', 1)[1]
    assert dockerfiles['frontend'].count(
        'FROM node:24.18.0-alpine@sha256:'
    ) == 1
    assert 'FROM deps AS dev' in dockerfiles['frontend']
    assert 'FROM deps AS builder' in dockerfiles['frontend']
    assert 'FROM base AS runner' in dockerfiles['frontend']
    assert 'npm-12.0.2.tgz' in dockerfiles['frontend']
    assert 'sha512sum -c -' in dockerfiles['frontend']
    assert NPM_TARBALL_SHA512 in dockerfiles['frontend']
    assert 'RUN npm ci --strict-allow-scripts' in dockerfiles['frontend']
    assert 'RUN node node_modules/next/dist/bin/next build' in (
        dockerfiles['frontend']
    )
    assert '\nUSER node\n' in frontend_runner
    assert 'rm -rf /usr/local/lib/node_modules/npm' in frontend_runner
    assert 'rm -f /usr/local/bin/npm /usr/local/bin/npx' in frontend_runner
    assert '--chown=node:node' in frontend_runner
    assert '/app/.next/standalone ./' in frontend_runner
    assert 'CMD ["node", "server.js"]' in frontend_runner
    assert '\nUSER postgres\n' in dockerfiles['backup']


def test_frontend_build_is_network_independent_and_install_scripts_are_reviewed():
    layout = (ROOT / 'frontend/src/app/layout.tsx').read_text()
    package = json.loads((ROOT / 'frontend/package.json').read_text())
    package_lock = json.loads((ROOT / 'frontend/package-lock.json').read_text())

    assert "from 'next/font/local'" in layout
    assert 'next/font/google' not in layout
    assert "src: './fonts/GeistVF.woff'" in layout
    assert (ROOT / 'frontend/src/app/fonts/GeistVF.woff').is_file()
    assert package['allowScripts'] == {
        'unrs-resolver@1.12.2': True,
        'fsevents': False,
    }
    assert package['scripts']['test:unit'] == 'node tests/run-unit-tests.mjs'
    test_runner = (ROOT / 'frontend/tests/run-unit-tests.mjs').read_text()
    assert "const outputRoot = join(frontendDirectory, '.test-dist')" in test_runner
    assert "mkdtempSync(join(outputRoot, 'run-'))" in test_runner
    assert "'--outDir'" in test_runner
    assert 'function findCompiledTests(directory)' in test_runner
    assert 'findCompiledTests(entryPath)' in test_runner
    assert "rmSync(outputDirectory, { force: true, recursive: true })" in test_runner
    assert "'--test-concurrency=1'" in test_runner
    assert "'--test-timeout=10000'" in test_runner
    assert "join(frontendDirectory, 'tsconfig.test.json')" in test_runner
    assert (ROOT / 'frontend/tsconfig.test.json').is_file()
    assert (ROOT / 'frontend/tests/browser-session-lock.test.ts').is_file()
    assert (ROOT / 'frontend/tests/billing-key-rotation.test.ts').is_file()
    assert (ROOT / 'frontend/tests/billing-concurrency.test.ts').is_file()
    assert (ROOT / 'frontend/tests/billing-page-loader.test.ts').is_file()
    assert (ROOT / 'frontend/tests/billing-timeouts.test.ts').is_file()
    assert (ROOT / 'frontend/tests/auth-csrf-retry.test.ts').is_file()
    assert (ROOT / 'frontend/tests/auth-cross-session.test.ts').is_file()
    assert (ROOT / 'frontend/tests/auth-refresh-rejection.test.ts').is_file()
    assert (ROOT / 'frontend/tests/auth-singleflight.test.ts').is_file()
    assert package_lock['packages']['node_modules/unrs-resolver']['version'] == '1.12.2'


def test_ci_actions_are_commit_pinned_and_job_is_bounded():
    action_specs = []
    for workflow in (CI_WORKFLOW, DEPLOY_WORKFLOW):
        action_specs.extend(
            re.findall(r'^\s*uses:\s*([^\s#]+)', workflow, re.MULTILINE)
        )

    assert action_specs
    for action in action_specs:
        _, separator, revision = action.rpartition('@')
        assert separator == '@', action
        assert re.fullmatch(r'[0-9a-f]{40}', revision), action

    assert 'permissions:\n  contents: read' in CI_WORKFLOW
    assert 'cancel-in-progress: true' in CI_WORKFLOW
    assert 'runs-on: ubuntu-24.04' in CI_WORKFLOW
    assert 'timeout-minutes: 60' in CI_WORKFLOW
    assert 'persist-credentials: false' in CI_WORKFLOW
    assert (
        'django celery_worker celery_beat celery_worker_images frontend backup nginx db'
        in CI_WORKFLOW
    )
    assert 'docker compose -f docker-compose.yml config --quiet' in CI_WORKFLOW
    assert 'trivy_0.72.0_Linux-64bit.tar.gz' in CI_WORKFLOW
    assert TRIVY_TARBALL_SHA256 in CI_WORKFLOW
    assert "'Version: 0.72.0'" in CI_WORKFLOW
    assert '--image-src docker' in CI_WORKFLOW
    assert '--disable-telemetry' in CI_WORKFLOW
    assert '--scanners vuln' in CI_WORKFLOW
    assert '--pkg-types os,library' in CI_WORKFLOW
    assert '--severity HIGH,CRITICAL' in CI_WORKFLOW
    assert '--ignore-unfixed' in CI_WORKFLOW
    assert '--ignorefile .trivyignore.yaml' in CI_WORKFLOW
    assert '--exit-code 1' in CI_WORKFLOW
    assert 'config --images "${scan_services[@]}"' in CI_WORKFLOW
    assert 'docker image inspect' in CI_WORKFLOW
    assert 'COMPOSE_PROJECT_NAME: saas-poster-ci-' in CI_WORKFLOW
    assert "python-version: '3.12.13'" in CI_WORKFLOW
    assert "node-version: '24.18.0'" in CI_WORKFLOW
    assert 'npm-12.0.2.tgz' in CI_WORKFLOW
    assert 'sha512sum -c -' in CI_WORKFLOW
    assert NPM_TARBALL_SHA512 in CI_WORKFLOW
    assert '--proto-redir \'=https\'' in CI_WORKFLOW
    assert '--requirement requirements/ci-tools.txt' in CI_WORKFLOW
    assert 'scripts/compile_requirements.sh' in CI_WORKFLOW
    assert 'shellcheck dev.sh deploy.sh' in CI_WORKFLOW
    assert 'scripts/ci_production_runtime_smoke.sh' in CI_WORKFLOW
    runtime_smoke = (
        ROOT / 'scripts/ci_production_runtime_smoke.sh'
    ).read_text()
    assert 'python manage.py check_public_http_connectivity' in runtime_smoke
    assert 'python scripts/ci_verify_egress_proxy.py' in runtime_smoke
    assert 'RUNNER_ENVIRONMENT:-' in runtime_smoke
    assert 'github-hosted' in runtime_smoke
    assert 'https://dodugir.com/api/v1/ready/' in runtime_smoke
    assert 'http://dodugir.com/' in runtime_smoke
    assert '/etc/letsencrypt' not in runtime_smoke
    egress_verifier = (ROOT / 'scripts/ci_verify_egress_proxy.py').read_text()
    assert 'ci-private-target.dodugir.com:443' in egress_verifier
    assert "'[::2]:443'" in egress_verifier
    assert 'git ls-files --error-unmatch' in CI_WORKFLOW
    assert 'git diff --exit-code -- "${lock_files[@]}"' in CI_WORKFLOW
    assert 'requirements/prod.txt' in CI_WORKFLOW
    assert 'requirements/dev.txt' in CI_WORKFLOW
    assert 'backup/requirements.txt' in CI_WORKFLOW
    assert CI_WORKFLOW.count('--require-hashes') >= 3
    assert CI_WORKFLOW.count('--only-binary=:all:') >= 2
    assert '--disable-pip' in CI_WORKFLOW
    assert 'python -m pip check' in CI_WORKFLOW
    assert 'npm audit --audit-level=moderate' in CI_WORKFLOW
    assert 'npm run audit:prod' in CI_WORKFLOW
    assert 'npm ci --strict-allow-scripts' in CI_WORKFLOW
    assert 'npm run typecheck' in CI_WORKFLOW
    assert 'npm run test:unit' in CI_WORKFLOW
    assert 'cyclonedx-py requirements' in CI_WORKFLOW
    assert 'npm sbom' in CI_WORKFLOW
    assert 'actions/upload-artifact@' in CI_WORKFLOW
    assert 'codecov/codecov-action@fb8b3582c8e4def4969c97caa2f19720cb33a72f' in (
        CI_WORKFLOW
    )
    assert 'version: v11.3.1' in CI_WORKFLOW


def test_trivy_exceptions_are_exact_path_scoped_and_expiring():
    exceptions = TRIVY_EXCEPTIONS['vulnerabilities']

    assert {item['id'] for item in exceptions} == EXPECTED_GOSU_EXCEPTIONS
    assert len(exceptions) == len(EXPECTED_GOSU_EXCEPTIONS)
    for item in exceptions:
        assert item['paths'] == ['usr/local/bin/gosu']
        assert str(item['expired_at']) == '2026-09-30'
        statement = item['statement'].lower()
        assert 'official postgresql' in statement
        assert 'uid/gid exec' in statement
        assert 'unreachable' in statement
        assert 'upstream rebuild' in statement


def test_ci_scans_every_unique_production_image_and_its_service_images_match_ci():
    scan_services_match = re.search(
        r'scan_services=\(\n(?P<services>(?:\s+[a-z_]+\n)+)\s+\)',
        CI_WORKFLOW,
    )
    assert scan_services_match
    scan_services = set(re.findall(r'[a-z_]+', scan_services_match['services']))
    production_image_services = {
        name
        for name, service in COMPOSE['services'].items()
        if 'image' in service or 'build' in service
    }

    assert scan_services == production_image_services
    ci_services = yaml.safe_load(CI_WORKFLOW)['jobs']['backend-quality']['services']
    postgres_dockerfile = ROOT / 'ops/postgres/Dockerfile'
    assert COMPOSE['services']['db']['build'] == {
        'context': '.',
        'dockerfile': 'ops/postgres/Dockerfile',
    }
    assert f"FROM {ci_services['db']['image']}" in postgres_dockerfile.read_text()
    assert ci_services['redis']['image'] == COMPOSE['services']['redis']['image']
    assert (
        RESTORE_COMPOSE['services']['egress_proxy']['build']
        == COMPOSE['services']['egress_proxy']['build']
        == {'context': '.', 'dockerfile': 'egress-proxy/Dockerfile'}
    )
    assert 'declare -A scanned_image_ids=()' in CI_WORKFLOW
    assert 'config --images restore' in CI_WORKFLOW


def test_ci_bootstraps_the_hash_locked_compatible_pip_toolchain():
    ci_tools_install = CI_WORKFLOW.index(
        '--requirement requirements/ci-tools.txt'
    )
    lock_recompile = CI_WORKFLOW.index('scripts/compile_requirements.sh')
    ci_tools_input = (ROOT / 'requirements/ci-tools.in').read_text()

    assert ci_tools_install < lock_recompile
    assert 'pip==26.2.1\n' in ci_tools_input
    assert 'pip-tools==7.6.1' in ci_tools_input
    assert 'pip install --upgrade' not in CI_WORKFLOW


def test_deploy_uses_native_ssh_with_verified_host_key_and_bounded_runtime():
    assert 'runs-on: ubuntu-24.04' in DEPLOY_WORKFLOW
    assert 'appleboy/ssh-action' not in DEPLOY_WORKFLOW
    assert 'ssh-keyscan -T 10 "$PROD_HOST"' in DEPLOY_WORKFLOW
    assert 'ssh-keygen -lf "$ssh_dir/candidate-host-key" -E sha256' in (
        DEPLOY_WORKFLOW
    )
    assert 'candidate_fingerprint" = "$PROD_HOST_FINGERPRINT"' in (
        DEPLOY_WORKFLOW
    )
    assert '-o BatchMode=yes' in DEPLOY_WORKFLOW
    assert '-o GlobalKnownHostsFile=/dev/null' in DEPLOY_WORKFLOW
    assert '-o IdentitiesOnly=yes' in DEPLOY_WORKFLOW
    assert '-o StrictHostKeyChecking=yes' in DEPLOY_WORKFLOW
    assert '-o UpdateHostKeys=no' in DEPLOY_WORKFLOW
    assert '-o UserKnownHostsFile="$ssh_dir/known_hosts"' in DEPLOY_WORKFLOW
    assert 'timeout --foreground --signal=TERM --kill-after=30s 140m' in (
        DEPLOY_WORKFLOW
    )
    assert 'deploy "$DEPLOY_SHA"' in DEPLOY_WORKFLOW
    assert "bash -se -- \"$DEPLOY_SHA\" <<'REMOTE'" not in DEPLOY_WORKFLOW
    assert 'test "$PROD_USER" = mapdeploy' in DEPLOY_WORKFLOW
    assert 'git merge-base --is-ancestor' not in DEPLOY_WORKFLOW


def test_dependabot_tracks_actions_images_and_application_dependencies():
    configured = {
        (update['package-ecosystem'], update['directory'])
        for update in DEPENDABOT['updates']
    }

    assert ('github-actions', '/') in configured
    assert ('docker', '/') in configured
    assert ('docker', '/frontend') in configured
    assert ('docker', '/backup') in configured
    assert ('docker-compose', '/') in configured
    assert ('pip', '/requirements') in configured
    assert ('pip', '/backup') in configured
    assert ('npm', '/frontend') in configured


def test_frontend_build_context_excludes_local_state_and_secrets():
    ignored = set(
        (ROOT / 'frontend/.dockerignore').read_text().splitlines()
    )

    for entry in (
        'node_modules',
        '.next',
        'coverage',
        '.git',
        '.env',
        '.env.*',
        '.npmrc',
        '.netrc',
        '*.pem',
        '*.key',
    ):
        assert entry in ignored


def test_backend_build_context_excludes_frontend_and_local_credentials():
    ignored = set((ROOT / '.dockerignore').read_text().splitlines())

    for entry in (
        '.github',
        '.env*',
        '**/.env*',
        '.npmrc',
        '**/.npmrc',
        '.netrc',
        '**/.netrc',
        '.pypirc',
        '.aws',
        '.ssh',
        '.docker',
        '.venv',
        'db.sqlite3',
        'local_settings.py',
        'restore.env',
        'frontend',
        '.pytest_cache',
        '.coverage*',
        'coverage',
        '*.log',
    ):
        assert entry in ignored


def test_edge_proxy_uses_read_only_config_and_sanitized_logs():
    nginx_volumes = COMPOSE['services']['nginx']['volumes']
    assert './nginx.conf:/etc/nginx/conf.d/default.conf:ro' in nginx_volumes
    assert 'static_volume:/app/staticfiles:ro' in nginx_volumes
    assert '/etc/letsencrypt:/etc/letsencrypt:ro' not in nginx_volumes
    assert any('/live/dodugir.com:' in volume for volume in nginx_volumes)
    assert any('/archive/dodugir.com:' in volume for volume in nginx_volumes)
    assert 'return 301 https://dodugir.com$request_uri;' in NGINX_CONFIG
    assert 'https://$host$request_uri' not in NGINX_CONFIG
    assert 'ssl_reject_handshake on;' in NGINX_CONFIG
    assert 'ssl_session_tickets off;' in NGINX_CONFIG
    assert 'log_format main_sanitized' in NGINX_CONFIG
    # nginx:alpine already defines this singleton directive in the enclosing
    # http block; repeating it in conf.d/default.conf prevents nginx startup.
    assert '\nkeepalive_timeout ' not in f'\n{NGINX_CONFIG}'
    assert '$args' not in NGINX_CONFIG
    assert '$http_referer' not in NGINX_CONFIG
    assert '$http_user_agent' not in NGINX_CONFIG
    assert 'limit_req zone=webhook' in NGINX_CONFIG
    assert 'limit_req zone=telegram_webhook burst=100' in NGINX_CONFIG

    assert 'strip_query_terms on' in SQUID_CONFIG
    assert 'httpd_suppress_version_string on' in SQUID_CONFIG
    assert 'http_access deny manager' in SQUID_CONFIG
    assert 'acl manager proto cache_object' not in SQUID_CONFIG
    assert 'localhost .localhost' not in SQUID_CONFIG
    assert 'logformat safe_access' in SQUID_CONFIG
    assert 'access_log /var/log/squid/access.log safe_access' in SQUID_CONFIG
    assert 'cache_log /var/log/squid/cache.log' in SQUID_CONFIG
    assert '%>rd:%>rP' in SQUID_CONFIG
    assert '%ru' not in SQUID_CONFIG
    assert '%rp' not in SQUID_CONFIG
    assert '%>rp' not in SQUID_CONFIG
    assert '%<rp' not in SQUID_CONFIG
    # Squid normalizes IPv4 destinations into this mapped range internally;
    # denying the /96 would therefore block all IPv4 egress, not just SSRF.
    assert 'acl blocked_destinations dst ::ffff:0:0/96' not in SQUID_CONFIG
    assert 'acl blocked_destinations dst 64:ff9b::/96' in SQUID_CONFIG
    assert 'acl blocked_destinations dst 2002::/16' in SQUID_CONFIG
    assert 'acl blocked_destinations dst ff00::/8' in SQUID_CONFIG


def test_smtp_egress_is_fixed_to_resend_submission_via_connect():
    for name in ('django', 'celery_worker', 'celery_beat', 'celery_worker_images'):
        assert COMPOSE['services'][name]['environment']['EMAIL_HTTP_PROXY_URL'] == (
            'http://egress_proxy:3128'
        )

    assert 'acl SSL_ports port 443 587' in SQUID_CONFIG
    assert 'acl Safe_ports port 80 443 587' in SQUID_CONFIG
    assert 'acl smtp_submission_port port 587' in SQUID_CONFIG
    assert 'acl platform_smtp dstdomain smtp.resend.com' in SQUID_CONFIG
    deny_non_connect = SQUID_CONFIG.index(
        'http_access deny smtp_submission_port !CONNECT'
    )
    deny_other_hosts = SQUID_CONFIG.index(
        'http_access deny smtp_submission_port !platform_smtp'
    )
    allow = SQUID_CONFIG.index('http_access allow all')
    assert deny_non_connect < deny_other_hosts < allow


def test_public_http_egress_uses_exact_proxy_with_final_destination_acl():
    proxy_url = 'http://egress_proxy:3128'
    for name in ('django', 'celery_worker', 'celery_beat', 'celery_worker_images'):
        service = COMPOSE['services'][name]
        assert service['environment']['PUBLIC_HTTP_PROXY_URL'] == proxy_url
        assert service['networks'] == ['backend']
        assert service['depends_on']['egress_proxy']['condition'] == 'service_healthy'

    assert COMPOSE['networks']['backend']['internal'] is True
    assert set(COMPOSE['services']['egress_proxy']['networks']) == {
        'backend',
        'egress_public',
    }
    assert 'acl blocked_destinations dst 127.0.0.0/8' in SQUID_CONFIG
    assert 'acl blocked_destinations dst 169.254.0.0/16' in SQUID_CONFIG
    assert 'acl blocked_destinations dst fc00::/7' in SQUID_CONFIG
    assert 'acl blocked_destinations dst ::/96' in SQUID_CONFIG
    for network in (
        '100::/8',
        '200::/7',
        '400::/6',
        '800::/5',
        '1000::/4',
        '2001::/23',
        '3fff::/20',
        '4000::/3',
        '6000::/3',
        '8000::/3',
        'a000::/3',
        'c000::/3',
        'e000::/4',
        'f000::/5',
        'f800::/6',
        'fe00::/9',
    ):
        assert f'acl blocked_destinations dst {network}' in SQUID_CONFIG
    deny_final_ip = SQUID_CONFIG.index('http_access deny blocked_destinations')
    allow_public = SQUID_CONFIG.index('http_access allow all')
    assert deny_final_ip < allow_public


def test_make_up_does_not_delete_host_wide_docker_cache():
    makefile = (ROOT / 'Makefile').read_text()
    up_recipe = makefile.split('\nup:\n', 1)[1].split('\n\n', 1)[0]

    assert ' prune ' not in up_recipe
    assert '$(COMPOSE) up -d' in up_recipe
    assert makefile.count('docker compose') == 1
    assert '--project-name saas_poster' in makefile
    assert '--project-directory "$(CURDIR)"' in makefile
    assert '-f "$(CURDIR)/docker-compose.yml"' in makefile
    assert 'NEXT_PUBLIC_API_URL="$${NEXT_PUBLIC_API_URL:-http://localhost:8000}"' in (
        makefile
    )
    assert 'PYTHON ?= python3' in makefile
    assert '$(PYTHON) -m pytest' in makefile
    assert '$(COMPOSE) exec django flake8 .' in makefile
    assert '$(COMPOSE) exec django mypy' in makefile
    assert 'mypy apps/' not in makefile


def test_make_bootstrap_prepares_database_before_application_rollout():
    makefile = (ROOT / 'Makefile').read_text()
    bootstrap = makefile.split('\nbootstrap:\n', 1)[1].split('\nup:\n', 1)[0]

    dependencies = bootstrap.index('up -d --wait --wait-timeout 120 db redis')
    migration = bootstrap.index('python manage.py migrate --noinput')
    seed = bootstrap.index('python manage.py seed_plans')
    periodic = bootstrap.index('python manage.py setup_periodic_tasks')
    rollout = bootstrap.index('up -d --build')

    assert dependencies < migration < seed < periodic < rollout
    assert 'run --rm --no-deps --build django' in bootstrap


def test_billing_is_fail_closed_in_base_settings_and_env_example():
    base_settings = (ROOT / 'config/settings/base.py').read_text()
    env_example = (ROOT / '.env.example').read_text()

    assert "os.environ.get('BILLING_ENABLED', 'false')" in base_settings
    assert '\nBILLING_ENABLED=false\n' in env_example


def test_dev_script_never_kills_foreign_processes_or_prunes_host_docker():
    script = (ROOT / 'dev.sh').read_text()

    assert 'for port in 3000 8000 5432 6379; do' in script
    assert 'require_free_port "$port"' in script
    assert script.index('for port in 3000 8000 5432 6379; do') > script.index(
        '"${COMPOSE[@]}" down --remove-orphans'
    )
    assert 'dev.sh не завершает чужие процессы' in script
    assert script.count('docker compose') == 1
    assert '--project-name saas_poster' in script
    assert '--project-directory "$ROOT_DIR"' in script
    assert '-f "$ROOT_DIR/docker-compose.yml"' in script
    assert '"${COMPOSE[@]}" down --remove-orphans' in script
    assert 'rm -rf "$ROOT_DIR/frontend/.next"' in script
    assert script.index('python manage.py migrate') < script.index(
        '"${COMPOSE[@]}" up -d django'
    )
    assert script.index('python manage.py seed_plans') < script.index(
        '"${COMPOSE[@]}" up -d django'
    )
    assert '/api/v1/ready/' in script
    assert script.index('trap cleanup EXIT') < script.index('npm run dev &')
    assert 'kill -TERM -- "-$FRONTEND_PID"' in script
    assert 'curl -sf --max-time 2 http://localhost:3000/' in script
    assert 'NEXT_PUBLIC_API_URL="${NEXT_PUBLIC_API_URL:-http://localhost:8000}"' in script
    assert script.index('Frontend готов.') < script.index('Проект запущен:')
    for unsafe_fragment in (
        'docker container prune',
        'docker image prune',
        'docker builder prune',
        'docker system prune',
        'kill -9',
        'lsof -ti',
        'docker compose down -v',
    ):
        assert unsafe_fragment not in script


def test_restore_runtime_is_separate_ephemeral_and_read_only():
    restore = RESTORE_COMPOSE['services']['restore']
    compose_source = (ROOT / 'docker-compose.restore.yml').read_text()
    wrapper = (ROOT / 'scripts/production_restore.sh').read_text()

    assert 'env_file' not in restore
    assert '.backup.env' not in compose_source
    assert 'BACKUP_SIGNING_PRIVATE_KEY' not in compose_source
    assert restore['depends_on'] == {
        'egress_proxy': {'condition': 'service_healthy'},
    }
    assert restore['restart'] == 'no'
    assert restore['read_only'] is True
    assert restore['cap_drop'] == ['ALL']
    assert restore['cap_add'] == ['DAC_READ_SEARCH']
    assert restore['security_opt'] == ['no-new-privileges:true']
    assert restore['volumes'][0] == 'restore_workspace:/tmp'
    assert restore['environment']['RESTORE_AGE_IDENTITY_FILE'].startswith(
        '/run/secrets/'
    )
    assert restore['environment']['HTTP_PROXY'] == 'http://egress_proxy:3128'
    assert 'saas-poster-restore' in wrapper
    assert 'RESTORE_LOCK_DIR="/run/lock/saas-poster"' in wrapper
    assert 'RESTORE_LOCK_FILE="$RESTORE_LOCK_DIR/restore.lock"' in wrapper
    assert '-O "$RESTORE_LOCK_DIR"' in wrapper
    assert 'must have mode 700' in wrapper
    assert 'exec 9>"$RESTORE_LOCK_FILE"' in wrapper
    assert 'exec 9<"$RESTORE_ENV_FILE"' not in wrapper
    assert 'flock -n 9' in wrapper
    assert 'down --volumes --remove-orphans' in wrapper
    assert 'restore cleanup failed' in wrapper
    assert 'build restore egress_proxy' in wrapper
    assert 'run --rm restore' in wrapper


def test_production_operator_scripts_pin_compose_scope_and_reload_tls():
    backup = (ROOT / 'scripts/production_backup.sh').read_text()
    backup_check = (ROOT / 'scripts/production_backup_check.sh').read_text()
    reload_nginx_path = ROOT / 'scripts/reload_production_nginx.sh'
    reload_nginx = reload_nginx_path.read_text()

    for script in (backup, backup_check, reload_nginx):
        assert script.count('docker compose') == 1
        assert '--project-name saas_poster' in script
        assert '--project-directory "$ROOT_DIR"' in script
        assert 'docker-compose.prod.yml' in script

    assert reload_nginx_path.stat().st_mode & 0o111
    assert 'ROOT_DIR="/opt/saas_poster"' in reload_nginx
    assert 'SAAS_POSTER_ROOT_DIR' not in reload_nginx
    assert 'nginx -t' in reload_nginx
    assert 'nginx -s reload' in reload_nginx
    assert 'scripts/reload_production_nginx.sh' in CI_WORKFLOW


def test_versioned_backup_lifecycle_expires_noncurrent_objects():
    rules = BACKUP_LIFECYCLE['Rules']

    assert rules
    for rule in rules:
        assert rule['Status'] == 'Enabled'
        assert rule['NoncurrentVersionExpiration']['NoncurrentDays'] > 0

    by_id = {rule['ID']: rule for rule in rules}
    for retention_class, days in (
        ('daily', 35),
        ('weekly', 100),
        ('monthly', 400),
    ):
        rule = by_id[f'expire-{retention_class}-database-backups']
        assert rule['Expiration']['Days'] == days
        assert rule['NoncurrentVersionExpiration']['NoncurrentDays'] >= days
    coverage = by_id['expire-old-coverage-markers']
    assert (
        coverage['NoncurrentVersionExpiration']['NoncurrentDays']
        >= coverage['Expiration']['Days']
    )


def test_media_lifecycle_preserves_current_objects_and_retains_old_versions():
    lifecycle = json.loads((ROOT / 'ops/s3/media-lifecycle.json').read_text())
    rules = lifecycle['Rules']

    assert len(rules) == 1
    rule = rules[0]
    assert rule['Status'] == 'Enabled'
    assert 'Expiration' not in rule
    assert rule['NoncurrentVersionExpiration']['NoncurrentDays'] >= 365
    assert rule['AbortIncompleteMultipartUpload']['DaysAfterInitiation'] <= 7


def test_every_production_service_has_cpu_memory_and_pid_guardrails():
    for name, service in COMPOSE['services'].items():
        assert float(service['cpus']) > 0, name
        assert service['mem_limit'], name
        assert 1 <= int(service['pids_limit']) <= 512, name

    assert '--concurrency=2' in COMPOSE['services']['celery_worker']['command']
    assert '--concurrency=1' in COMPOSE['services']['celery_worker_images']['command']
    assert '-w 2' in COMPOSE['services']['django']['command']
