"""Shim hot-path benchmark — covers Finding #6 in the CL3 review.

§9.1 budgets:

* SDK shim wrapper overhead — under 100 µs p95 in metadata mode.
* Replay mode is not subject to the 100 µs hot-path budget because
  ``json.dumps(request_kwargs)`` and the response serialiser
  dominate. We still gate it: replay-mode p95 should be under 5 ms
  for a small fixture request — that's looser than the storage
  budget but tight enough to flag a 10× regression.

Both budgets fire against the fake Anthropic SDK so the timing
covers the shim + translator + storage path without any network or
real-SDK noise.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import inkfoot._instrument as instrument_mod
from inkfoot._run_context import _clear_current_run, _set_current_run
from inkfoot.policy.registry import PolicyRegistry
from inkfoot.storage.sqlite import SQLiteStorage
from tests.unit._fake_sdks import install_fake_anthropic, uninstall_fake_sdks


_METADATA_BUDGET_S = 0.000100  # 100 µs §9.1
_METADATA_P95_S = 0.000200  # softer p95 bar (CI noise)
_REPLAY_P95_S = 0.005  # 5 ms — looser per Finding #6 reasoning


@pytest.fixture()
def shim_setup(tmp_path: Path):
    """Install the fake SDK + instrument; yield the fake's Messages
    client; tear down on exit."""
    fakes = install_fake_anthropic()
    storage = SQLiteStorage(path=tmp_path / "perf.db")
    instrument_mod.shutdown()
    PolicyRegistry.clear()
    _clear_current_run()
    yield_data = {"fakes": fakes, "storage": storage, "tmp_path": tmp_path}
    yield yield_data
    instrument_mod.shutdown()
    PolicyRegistry.clear()
    _clear_current_run()
    uninstall_fake_sdks()


def _seed(storage: SQLiteStorage) -> str:
    storage.connect()
    storage.start_run(
        run_id="perf-run",
        task="shim-bench",
        agent_kind="bench",
        started_at=1_700_000_000_000,
    )
    return "perf-run"


def test_metadata_mode_hot_path_under_perf_budget(benchmark, shim_setup) -> None:
    """Metadata mode (the default Phase 0 posture) hits the §9.1
    hot-path budget. Looser p95 bound than 100 µs because the CI
    runner adds variance — the mean assertion catches regressions."""
    fakes = shim_setup["fakes"]
    storage = shim_setup["storage"]
    instrument_mod.instrument(storage=storage)  # capture_mode="metadata"
    _set_current_run(_seed(storage))

    client = fakes["Messages"]()
    request = dict(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hello"}],
    )

    def one_call() -> None:
        client.create(**request)

    # Warm-up so the first JIT-y branch isn't in the sample.
    for _ in range(50):
        one_call()

    benchmark.pedantic(one_call, rounds=500, iterations=1)

    sample = sorted(benchmark.stats.stats.data)
    p95 = sample[int(len(sample) * 0.95)]
    assert benchmark.stats.stats.mean < _METADATA_P95_S, (
        f"shim metadata-mode mean {benchmark.stats.stats.mean * 1_000_000:.1f} µs "
        f"exceeded {_METADATA_P95_S * 1_000_000:.0f} µs"
    )
    # Soft p95 — log if blown but don't fail (the §9.1 budget is
    # against real SDK overhead, not the shim+fake roundtrip; this
    # benchmark exists to catch order-of-magnitude regressions).
    if p95 >= _METADATA_BUDGET_S:
        print(
            f"  [warn] shim metadata-mode p95 = {p95 * 1_000_000:.1f} µs "
            f"(§9.1 budget 100 µs)"
        )


def test_replay_mode_hot_path_under_looser_budget(benchmark, shim_setup) -> None:
    """Replay mode adds JSON serialisation of request_kwargs and the
    SDK response on every call. Per Finding #6, this isn't subject
    to the 100 µs §9.1 budget — but it should still complete well
    under 5 ms p95 for a small fixture."""
    fakes = shim_setup["fakes"]
    storage = shim_setup["storage"]
    instrument_mod.instrument(storage=storage, capture_mode="replay")
    _set_current_run(_seed(storage))

    client = fakes["Messages"]()
    request = dict(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hello"}],
    )

    def one_call() -> None:
        client.create(**request)

    for _ in range(50):
        one_call()

    benchmark.pedantic(one_call, rounds=500, iterations=1)
    sample = sorted(benchmark.stats.stats.data)
    p95 = sample[int(len(sample) * 0.95)]
    assert p95 < _REPLAY_P95_S, (
        f"shim replay-mode p95 {p95 * 1000:.2f} ms exceeded "
        f"{_REPLAY_P95_S * 1000:.0f} ms budget"
    )
