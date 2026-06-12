"""Deterministic self-check scenario for the contract-check CI job.

Real benchmark scenarios make live LLM calls, which CI can't do on
every push. This scenario instead emits two synthetic ``llm_call``
events with fixed costs straight into the runner's storage, producing
a byte-stable artefact (two calls, 3,000,000 nanodollars per run).
That is enough to prove the whole gate end to end: scenario discovery
→ ``agent_run`` capture → artefact aggregation → ``inkfoot contract
check`` verdict. The companion contract in ``../contracts`` sets its
ceilings around these constants, so a pipeline regression — not cost
noise — is the only thing that can flip the gate.
"""

import json

_CALLS_PER_RUN = 2
_NANODOLLARS_PER_CALL = 1_500_000

INKFOOT_SCENARIO = {
    "task": "pipeline-smoke",
    "fixtures": [],
    "runs_per_fixture": 1,
    "expected_outcome": "success",
}


def run(fixture):
    from ulid import ULID

    from inkfoot import _instrument as _inst
    from inkfoot._run_context import current_run_id
    from inkfoot.shims._emit import _next_sequence

    run_id = current_run_id()
    assert run_id is not None, "run() must execute inside agent_run"
    storage = _inst._STORAGE
    assert storage is not None, "the benchmark runner boots instrument()"

    for i in range(_CALLS_PER_RUN):
        payload = {
            "provider": "anthropic",
            "model": "claude-haiku-4-5",
            "system_static_tokens": 120,
            "user_input_tokens": 80,
            "output_tokens": 40,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "estimated_nanodollars": _NANODOLLARS_PER_CALL,
            "metadata": {"synthetic": True},
        }
        storage.insert_event(
            event_id=str(ULID()),
            run_id=run_id,
            kind="llm_call",
            occurred_at=1_700_000_000_000 + i,
            sequence=_next_sequence(run_id),
            payload_json=json.dumps(payload),
            capture_mode="metadata",
        )
