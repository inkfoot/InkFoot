"""Capability-matrix tests for the modification policies.

``LazyToolExposure`` and ``CheapSummariser`` declare
``SUPPORTED_PATTERNS = {C}``: registering them under the SDK shim
(Pattern A) or the raw decorator (Pattern B) must raise
:class:`PolicyNotSupported` with a remediation hint, while the
framework adapters enumerate them in ``supported_policies()``.
"""

from __future__ import annotations

import pytest

import inkfoot
import inkfoot._instrument as instrument_mod
from inkfoot.errors import PolicyNotSupported
from inkfoot.policy import (
    BudgetCap,
    CheapSummariser,
    IntegrationPattern,
    LazyToolExposure,
    Policy,
    register_policies,
)
from inkfoot.policy.registry import PolicyRegistry
from inkfoot.storage.sqlite import SQLiteStorage
from tests.unit._fake_sdks import install_fake_anthropic, uninstall_fake_sdks


@pytest.fixture(autouse=True)
def clean_state() -> None:
    instrument_mod.shutdown()
    PolicyRegistry.clear()
    uninstall_fake_sdks()
    yield
    instrument_mod.shutdown()
    PolicyRegistry.clear()
    uninstall_fake_sdks()


@pytest.mark.parametrize("policy_cls", [LazyToolExposure, CheapSummariser])
def test_pattern_a_registration_raises(policy_cls: type[Policy]) -> None:
    with pytest.raises(PolicyNotSupported) as exc:
        register_policies(
            [policy_cls()], active_pattern=IntegrationPattern.A
        )
    message = str(exc.value)
    assert policy_cls.__name__ in message
    assert "Pattern A" in message
    assert "inkfoot.dev/docs/policies" in message
    assert len(PolicyRegistry) == 0


@pytest.mark.parametrize("policy_cls", [LazyToolExposure, CheapSummariser])
def test_pattern_b_registration_raises(policy_cls: type[Policy]) -> None:
    with pytest.raises(PolicyNotSupported):
        register_policies(
            [policy_cls()], active_pattern=IntegrationPattern.B
        )
    assert len(PolicyRegistry) == 0


def test_pattern_c_registration_succeeds() -> None:
    register_policies(
        [LazyToolExposure(), CheapSummariser()],
        active_pattern=IntegrationPattern.C,
    )
    assert len(PolicyRegistry) == 2


def test_instrument_with_modification_policy_raises_before_shim_install(
    tmp_path,
) -> None:
    """``inkfoot.instrument()`` is the Pattern-A entry point — passing
    a modification policy must fail fast, before any shim installs."""
    install_fake_anthropic()
    storage = SQLiteStorage(path=tmp_path / "runs.db")

    with pytest.raises(PolicyNotSupported, match="LazyToolExposure"):
        inkfoot.instrument(storage=storage, policies=[LazyToolExposure()])

    from inkfoot._shim_install import installed_providers

    assert installed_providers() == []
    assert instrument_mod.is_instrumented() is False


class _AdapterStub:
    """Minimal adapter surface for the adapter-gated path."""

    name = "stub"

    def __init__(self, supported: set[type]) -> None:
        self._supported = supported

    def supported_policies(self) -> set[type]:
        return self._supported


def test_adapter_that_enumerates_policies_accepts_them() -> None:
    adapter = _AdapterStub({LazyToolExposure, CheapSummariser})
    register_policies(
        [LazyToolExposure(), CheapSummariser()], adapter=adapter
    )
    assert len(PolicyRegistry) == 2


def test_adapter_without_enumeration_rejects_modification_policy() -> None:
    adapter = _AdapterStub(set())
    with pytest.raises(PolicyNotSupported) as exc:
        register_policies([CheapSummariser()], adapter=adapter)
    message = str(exc.value)
    assert "CheapSummariser" in message
    assert "stub" in message


def test_adapter_path_still_accepts_observation_policies() -> None:
    """Observation policies pass the adapter gate via the
    all-patterns fallback even when not enumerated."""
    adapter = _AdapterStub(set())
    register_policies([BudgetCap(max_nd=10**9)], adapter=adapter)
    assert len(PolicyRegistry) == 1


@pytest.mark.parametrize(
    "adapter_path",
    [
        "inkfoot.adapters.langgraph.LangGraphAdapter",
        "inkfoot.adapters.openai_agents.OpenAIAgentsAdapter",
        "inkfoot.adapters.anthropic_agent.AnthropicAgentAdapter",
    ],
)
def test_real_adapters_enumerate_both_modification_policies(
    adapter_path: str,
) -> None:
    module_path, cls_name = adapter_path.rsplit(".", 1)
    module = __import__(module_path, fromlist=[cls_name])
    adapter = getattr(module, cls_name)()
    assert adapter.supported_policies() == {LazyToolExposure, CheapSummariser}
