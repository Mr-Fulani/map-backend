from pathlib import Path

import pytest

from scripts.ci_pytest_shard import discover_test_files, split_test_files


ROOT = Path(__file__).resolve().parents[1]


def _test_file(root: Path, path: str, size: int) -> Path:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('x' * size)
    return target.relative_to(root)


def test_discovery_and_sharding_cover_every_test_file_exactly_once(tmp_path):
    expected = {
        _test_file(tmp_path, 'apps/one/tests/test_large.py', 100),
        _test_file(tmp_path, 'apps/two/tests/test_small.py', 10),
        _test_file(tmp_path, 'tests/test_root.py', 40),
    }
    _test_file(tmp_path, 'apps/one/tests/helper.py', 500)

    discovered = discover_test_files(tmp_path)
    first = split_test_files(tmp_path, discovered, 2)
    second = split_test_files(tmp_path, discovered, 2)

    assert first == second
    assert {path for shard in first for path in shard} == expected
    assert sum(len(shard) for shard in first) == len(expected)
    assert all(shard for shard in first)


def test_sharding_rejects_a_non_positive_count(tmp_path):
    with pytest.raises(ValueError, match='positive'):
        split_test_files(tmp_path, [], 0)


def test_repository_shards_cover_every_backend_test_file_once():
    discovered = discover_test_files(ROOT)
    shards = split_test_files(ROOT, discovered, 3)
    flattened = [path for shard in shards for path in shard]
    weights = [sum((ROOT / path).stat().st_size for path in shard) for shard in shards]

    assert len(discovered) > 150
    assert len(flattened) == len(discovered)
    assert set(flattened) == set(discovered)
    assert max(weights) - min(weights) < max(weights) * 0.05
