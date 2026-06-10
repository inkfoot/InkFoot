"""Token Contracts — declarative, version-controlled budget and outcome
expectations for an agent task.

A contract is a small YAML file that states the budget a task should
hold to, the quality outcome it's expected to deliver, and a *degrade
ladder* describing what the runtime should do as a run approaches its
ceiling. Contracts are loaded at :func:`inkfoot.instrument` time and
enforced on the LLM-call hot path; they are also checked in CI against
a benchmark artefact so a budget regression fails the build before it
ships.

The public surface is intentionally small:

* :func:`load_contract`, :func:`load_contracts`, :func:`load_contracts_dir`
  — read and validate contract files.
* :class:`Contract` and friends — the typed, validated schema.
* :class:`ContractEnforcer` — the storage-agnostic decision engine.
* :class:`ContractValidationError` — raised for any malformed file.
"""

from __future__ import annotations

from inkfoot.contracts.enforcer import (
    ContractEnforcer,
    ContractViolation,
    EnforcementOutcome,
)
from inkfoot.contracts.loader import (
    load_contract,
    load_contracts,
    load_contracts_dir,
)
from inkfoot.contracts.schema import (
    CONTRACT_SCHEMA_VERSION,
    BudgetClause,
    Contract,
    ContractValidationError,
    DegradeAction,
    DegradeStep,
    OutcomeClause,
    Override,
    contract_from_dict,
)

__all__ = [
    "CONTRACT_SCHEMA_VERSION",
    "BudgetClause",
    "Contract",
    "ContractEnforcer",
    "ContractValidationError",
    "ContractViolation",
    "DegradeAction",
    "DegradeStep",
    "EnforcementOutcome",
    "OutcomeClause",
    "Override",
    "contract_from_dict",
    "load_contract",
    "load_contracts",
    "load_contracts_dir",
]
