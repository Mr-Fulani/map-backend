#!/usr/bin/env python3
"""Split all pytest files deterministically across CI workers."""

from __future__ import annotations

import argparse
from pathlib import Path


def discover_test_files(root: Path) -> list[Path]:
    files = {
        path.relative_to(root)
        for base in (root / 'apps', root / 'tests')
        if base.exists()
        for path in base.rglob('test_*.py')
        if path.is_file()
    }
    return sorted(files, key=lambda path: path.as_posix())


def split_test_files(
    root: Path,
    files: list[Path],
    shard_count: int,
) -> list[list[Path]]:
    if shard_count < 1:
        raise ValueError('shard_count must be positive')
    shards: list[list[Path]] = [[] for _ in range(shard_count)]
    weights = [0] * shard_count
    weighted_files = sorted(
        (((root / path).stat().st_size, path) for path in files),
        key=lambda item: (-item[0], item[1].as_posix()),
    )
    for weight, path in weighted_files:
        shard_index = min(range(shard_count), key=lambda index: (weights[index], index))
        shards[shard_index].append(path)
        weights[shard_index] += weight
    return [sorted(shard, key=lambda path: path.as_posix()) for shard in shards]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', default='.')
    parser.add_argument('--shard-index', required=True, type=int)
    parser.add_argument('--shard-count', required=True, type=int)
    args = parser.parse_args()

    root = Path(args.repo).resolve()
    files = discover_test_files(root)
    shards = split_test_files(root, files, args.shard_count)
    if not 0 <= args.shard_index < len(shards):
        parser.error('shard-index must be zero-based and smaller than shard-count')
    selected = shards[args.shard_index]
    if not selected:
        parser.error('selected shard is empty')
    for path in selected:
        print(path.as_posix())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
