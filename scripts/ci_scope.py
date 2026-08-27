#!/usr/bin/env python3
"""Classify a CI run and reuse only a proven full gate for the exact tree."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import PurePosixPath
from typing import Callable


CI_WORKFLOW_PATH = '.github/workflows/ci.yml'
FULL_GATE_PREFIX = 'ci-gate-'
ZERO_SHA = '0' * 40


def is_docs_path(path: str) -> bool:
    """Return true only for Markdown in docs/ or at repository root."""
    candidate = PurePosixPath(path)
    if candidate.suffix.lower() != '.md':
        return False
    return len(candidate.parts) == 1 or candidate.parts[0] == 'docs'


def classify_paths(paths: list[str]) -> str:
    """Fail closed when a diff is empty or includes any non-docs path."""
    if paths and all(is_docs_path(path) for path in paths):
        return 'docs'
    return 'full'


def _git(repo: str, *args: str, text: bool = True) -> str | bytes:
    result = subprocess.run(
        ['git', '-C', repo, *args],
        check=True,
        capture_output=True,
        text=text,
    )
    return result.stdout


def changed_paths(repo: str, base_sha: str, head_sha: str) -> list[str]:
    output = _git(
        repo,
        'diff',
        '--name-only',
        '--no-renames',
        '--diff-filter=ACDMRTUXB',
        '-z',
        base_sha,
        head_sha,
        text=False,
    )
    return [os.fsdecode(path) for path in output.split(b'\0') if path]


def checked_tree(repo: str) -> str:
    return str(_git(repo, 'rev-parse', 'HEAD^{tree}')).strip()


def _api_json(
    url: str,
    token: str,
    *,
    opener: Callable[..., object] = urllib.request.urlopen,
) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            'Accept': 'application/vnd.github+json',
            'Authorization': f'Bearer {token}',
            'X-GitHub-Api-Version': '2026-03-10',
        },
    )
    with opener(request, timeout=15) as response:
        return json.load(response)


def _verified_run(run: dict, repository: str) -> bool:
    workflow_path = str(run.get('path', '')).split('@', 1)[0]
    run_repository = run.get('repository') or {}
    head_repository = run.get('head_repository') or {}
    return (
        run.get('status') == 'completed'
        and run.get('conclusion') == 'success'
        and workflow_path == CI_WORKFLOW_PATH
        and run.get('event') in {'pull_request', 'push'}
        and run_repository.get('full_name', '').casefold() == repository.casefold()
        and head_repository.get('full_name', '').casefold() == repository.casefold()
    )


def has_verified_full_gate(
    *,
    api_url: str,
    repository: str,
    token: str,
    tree_sha: str,
    current_run_id: int,
    opener: Callable[..., object] = urllib.request.urlopen,
) -> bool:
    """Find a completed successful full-gate artifact for the exact tree."""
    artifact_name = f'{FULL_GATE_PREFIX}{tree_sha}-full'
    query = urllib.parse.urlencode({'name': artifact_name, 'per_page': 100})
    artifacts_url = f'{api_url}/repos/{repository}/actions/artifacts?{query}'
    payload = _api_json(artifacts_url, token, opener=opener)

    for artifact in payload.get('artifacts', []):
        if artifact.get('expired') or artifact.get('name') != artifact_name:
            continue
        run_id = artifact.get('workflow_run', {}).get('id')
        if not isinstance(run_id, int) or run_id == current_run_id:
            continue
        run_url = f'{api_url}/repos/{repository}/actions/runs/{run_id}'
        run = _api_json(run_url, token, opener=opener)
        if _verified_run(run, repository):
            return True
    return False


def event_range(event: dict, event_name: str) -> tuple[str, str]:
    if event_name == 'pull_request':
        pull_request = event['pull_request']
        return pull_request['base']['sha'], pull_request['head']['sha']
    if event_name == 'push':
        return event.get('before', ZERO_SHA), event['after']
    raise ValueError(f'unsupported CI event: {event_name}')


def write_outputs(path: str, values: dict[str, str]) -> None:
    with open(path, 'a', encoding='utf-8') as output:
        for key, value in values.items():
            if '\n' in value or '\r' in value:
                raise ValueError(f'invalid multiline output: {key}')
            output.write(f'{key}={value}\n')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--event-file', required=True)
    parser.add_argument('--event-name', required=True)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--run-id', required=True, type=int)
    parser.add_argument('--repo', default='.')
    args = parser.parse_args()

    with open(args.event_file, encoding='utf-8') as event_stream:
        event = json.load(event_stream)
    base_sha, head_sha = event_range(event, args.event_name)
    paths = (
        []
        if base_sha == ZERO_SHA
        else changed_paths(args.repo, base_sha, head_sha)
    )
    mode = classify_paths(paths)
    tree_sha = checked_tree(args.repo)

    if mode == 'full' and args.event_name == 'push':
        token = os.environ.get('ACTIONS_READ_TOKEN', '')
        api_url = os.environ.get('GITHUB_API_URL', 'https://api.github.com')
        if token:
            try:
                if has_verified_full_gate(
                    api_url=api_url,
                    repository=args.repository,
                    token=token,
                    tree_sha=tree_sha,
                    current_run_id=args.run_id,
                ):
                    mode = 'reuse'
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
                print(
                    f'CI gate lookup failed closed; running full CI: {error}',
                    file=sys.stderr,
                )

    outputs = {
        'base_sha': base_sha,
        'head_sha': head_sha,
        'mode': mode,
        'tree_sha': tree_sha,
    }
    output_path = os.environ.get('GITHUB_OUTPUT')
    if output_path:
        write_outputs(output_path, outputs)
    print(json.dumps({**outputs, 'changed_file_count': len(paths)}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
