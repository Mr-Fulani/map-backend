import io
import json

from scripts.ci_scope import (
    ZERO_SHA,
    classify_paths,
    event_range,
    has_verified_full_gate,
    is_docs_path,
)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def _opener(payloads):
    responses = iter(payloads)

    def open_request(_request, timeout):
        assert timeout == 15
        return FakeResponse(json.dumps(next(responses)).encode())

    return open_request


def test_docs_scope_is_narrow_and_fails_closed():
    assert is_docs_path('README.md')
    assert is_docs_path('docs/operations/runbook.md')
    assert not is_docs_path('.github/PULL_REQUEST_TEMPLATE.md')
    assert not is_docs_path('docs/generated/config.json')
    assert classify_paths(['README.md', 'docs/DEPLOYMENT.md']) == 'docs'
    assert classify_paths([]) == 'full'
    assert classify_paths(['docs/DEPLOYMENT.md', '.env.example']) == 'full'


def test_event_range_uses_exact_pull_request_or_push_boundaries():
    pull_request = {'pull_request': {'base': {'sha': 'a'}, 'head': {'sha': 'b'}}}
    push = {'before': 'c', 'after': 'd'}

    assert event_range(pull_request, 'pull_request') == ('a', 'b')
    assert event_range(push, 'push') == ('c', 'd')
    assert event_range({'after': 'd'}, 'push') == (ZERO_SHA, 'd')


def test_exact_tree_reuse_requires_a_successful_repo_owned_ci_run():
    tree = 'a' * 40
    artifacts = {
        'artifacts': [
            {
                'expired': False,
                'name': f'ci-gate-{tree}-full',
                'workflow_run': {'id': 42},
            }
        ]
    }
    run = {
        'status': 'completed',
        'conclusion': 'success',
        'path': '.github/workflows/ci.yml@refs/pull/7/merge',
        'event': 'pull_request',
        'repository': {'full_name': 'owner/repo'},
        'head_repository': {'full_name': 'owner/repo'},
    }

    assert has_verified_full_gate(
        api_url='https://api.github.test',
        repository='owner/repo',
        token='not-a-real-token',
        tree_sha=tree,
        current_run_id=99,
        opener=_opener([artifacts, run]),
    )


def test_exact_tree_reuse_rejects_failed_or_foreign_runs():
    tree = 'b' * 40
    artifacts = {
        'artifacts': [
            {
                'expired': False,
                'name': f'ci-gate-{tree}-full',
                'workflow_run': {'id': 42},
            }
        ]
    }
    foreign_run = {
        'status': 'completed',
        'conclusion': 'success',
        'path': '.github/workflows/ci.yml',
        'event': 'pull_request',
        'repository': {'full_name': 'owner/repo'},
        'head_repository': {'full_name': 'fork/repo'},
    }

    assert not has_verified_full_gate(
        api_url='https://api.github.test',
        repository='owner/repo',
        token='not-a-real-token',
        tree_sha=tree,
        current_run_id=99,
        opener=_opener([artifacts, foreign_run]),
    )
