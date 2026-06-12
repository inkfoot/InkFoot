"""Adapter × provider compatibility matrix.

Two cross-cutting guarantees, pinned in one place:

1. **Pairwise invariants** — every framework adapter must be usable
   in front of every shipped provider. The join point is the policy
   layer: an adapter that supports modification policies can only
   deliver them when the provider's declared capabilities make each
   policy applicable. Asserting the full cross product here means a
   new provider (or a capability downgrade) that would strand an
   adapter's policies fails CI instead of a user's integration.
2. **Documentation drift guard** — the published capability matrix
   (docs/reference/provider-matrix.md) promises it is CI-checked
   against the live declarations. This file is that check: the table
   is parsed cell-by-cell and compared against the ``Capabilities``
   records the providers actually ship, including Bedrock's
   per-model-family prefix routing.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

from inkfoot.policy import CheapSummariser, LazyToolExposure
from inkfoot.providers.anthropic import AnthropicProvider
from inkfoot.providers.base import PROMPT_CACHE_STYLES, Capabilities, TokenUsage
from inkfoot.providers.bedrock import (
    _ANTHROPIC_FAMILY_CAPS,
    _BEDROCK_MODEL_CAPS,
    _NO_CACHE_FAMILY_CAPS,
    BedrockProvider,
)
from inkfoot.providers.gemini import GeminiProvider
from inkfoot.providers.openai import OpenAIProvider
from inkfoot.providers.openai_compat import (
    _CONSERVATIVE_COMPAT_CAPS,
    OpenAICompatProvider,
)
from tests.contract.test_framework_adapter_contract import SPECS

_DOC_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "reference"
    / "provider-matrix.md"
)


@dataclass(frozen=True)
class ProviderColumn:
    """One column of the published matrix and its live counterpart."""

    column: str  # exact column header in the docs table
    provider_type: str
    caps: Capabilities


PROVIDER_COLUMNS = [
    ProviderColumn(
        column="Anthropic",
        provider_type=AnthropicProvider.PROVIDER_TYPE,
        caps=AnthropicProvider.CAPABILITIES,
    ),
    ProviderColumn(
        column="OpenAI",
        provider_type=OpenAIProvider.PROVIDER_TYPE,
        caps=OpenAIProvider.CAPABILITIES,
    ),
    ProviderColumn(
        column="Gemini",
        provider_type=GeminiProvider.PROVIDER_TYPE,
        caps=GeminiProvider.CAPABILITIES,
    ),
    ProviderColumn(
        column="Bedrock (Anthropic models)",
        provider_type=BedrockProvider.PROVIDER_TYPE,
        caps=_ANTHROPIC_FAMILY_CAPS,
    ),
    ProviderColumn(
        column="Bedrock (other families)",
        provider_type=BedrockProvider.PROVIDER_TYPE,
        caps=_NO_CACHE_FAMILY_CAPS,
    ),
    ProviderColumn(
        column="OpenAI-compatible (default)",
        provider_type=OpenAICompatProvider.PROVIDER_TYPE,
        caps=_CONSERVATIVE_COMPAT_CAPS,
    ),
]

_COLUMN_PARAMS = [pytest.param(c, id=c.column) for c in PROVIDER_COLUMNS]
_ADAPTER_PARAMS = [pytest.param(s, id=s.name) for s in SPECS]


def _provider_instances():
    return [
        AnthropicProvider(),
        OpenAIProvider(),
        GeminiProvider(),
        BedrockProvider(),
        OpenAICompatProvider(base_url="http://localhost:11434/v1", model="llama3.2"),
    ]


# ----------------------------------------------------------------------
# Pairwise invariants
# ----------------------------------------------------------------------


@pytest.mark.parametrize("column", _COLUMN_PARAMS)
@pytest.mark.parametrize("spec", _ADAPTER_PARAMS)
def test_adapter_policies_are_applicable_on_every_provider(spec, column) -> None:
    module = importlib.import_module(spec.adapter_module)
    adapter = getattr(module, spec.adapter_cls)()
    policies = adapter.supported_policies()

    if spec.observation_only:
        # Observation-only adapters place no demands on a provider's
        # request shape, so any provider pairing is trivially valid.
        assert policies == set()
        return

    # LazyToolExposure rewrites the request's tools array, so every
    # provider an adapter can face must accept one.
    if LazyToolExposure in policies:
        assert column.caps.supports_tool_use, (
            f"{spec.name} supports LazyToolExposure but provider column "
            f"{column.column!r} declares no tool use"
        )

    # CheapSummariser either calls the provider's declared cheap tier
    # or falls back to truncation when there is none (None). An empty
    # or non-string declaration would break its model routing.
    if CheapSummariser in policies:
        cheap = column.caps.cheap_model_for_summariser
        assert cheap is None or (isinstance(cheap, str) and cheap), (
            f"provider column {column.column!r} declares an unusable "
            f"cheap summariser model {cheap!r}"
        )


@pytest.mark.parametrize(
    "provider",
    _provider_instances(),
    ids=lambda p: type(p).__name__,
)
def test_map_usage_tolerates_error_shaped_responses(provider) -> None:
    # Adapters hand whatever the wrapped SDK produced — including
    # nothing at all on an errored call — to map_usage. The provider
    # contract is to zero out rather than raise, for every pairing.
    for response in (None, {}, {"usage": None}):
        usage = provider.map_usage(response)
        assert isinstance(usage, TokenUsage)
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0
        assert usage.cache_read_tokens == 0
        assert usage.cache_creation_tokens == 0


@pytest.mark.parametrize("column", _COLUMN_PARAMS)
def test_declared_capabilities_are_coherent(column) -> None:
    caps = column.caps
    assert caps.prompt_cache_style in PROMPT_CACHE_STYLES
    assert caps.supports_prompt_cache == (caps.prompt_cache_style != "none")
    assert caps.cache_read_price_ratio >= 0
    assert caps.cache_write_price_ratio >= 0


# ----------------------------------------------------------------------
# Documentation drift guard
# ----------------------------------------------------------------------


def _parse_pipe_tables(text: str) -> list[list[list[str]]]:
    """Return every Markdown pipe table as a list of cell-rows,
    separator rows dropped."""
    tables: list[list[list[str]]] = []
    current: list[list[str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(re.fullmatch(r":?-{3,}:?", c) for c in cells):
                continue  # header separator row
            current.append(cells)
        elif current:
            tables.append(current)
            current = []
    if current:
        tables.append(current)
    return tables


def _matrix_table() -> dict[str, dict[str, str]]:
    """The capability table as ``{column: {row_label: cell}}``."""
    tables = _parse_pipe_tables(_DOC_PATH.read_text(encoding="utf-8"))
    assert tables, f"no tables found in {_DOC_PATH}"
    header, *rows = tables[0]
    assert header[0] == "Capability"
    out: dict[str, dict[str, str]] = {col: {} for col in header[1:]}
    for row in rows:
        label = row[0]
        for col, cell in zip(header[1:], row[1:]):
            out[col][label] = cell
    return out


def _unticked(cell: str) -> str:
    return cell.strip("`")


def _as_bool(cell: str) -> bool:
    assert cell in ("yes", "no"), f"expected yes/no cell, got {cell!r}"
    return cell == "yes"


@pytest.mark.parametrize("column", _COLUMN_PARAMS)
def test_documented_matrix_matches_live_declarations(column) -> None:
    table = _matrix_table()
    assert column.column in table, (
        f"column {column.column!r} missing from {_DOC_PATH}"
    )
    doc = table[column.column]
    caps = column.caps

    assert _unticked(doc["Provider string"]) == column.provider_type
    assert _as_bool(doc["Tool use"]) == caps.supports_tool_use
    assert _as_bool(doc["Image input"]) == caps.supports_image_input
    assert _as_bool(doc["Document blocks"]) == caps.supports_document_block
    assert _as_bool(doc["Prompt caching"]) == caps.supports_prompt_cache
    assert _unticked(doc["Cache style"]) == caps.prompt_cache_style
    assert float(doc["Cache read price ratio"]) == caps.cache_read_price_ratio
    assert float(doc["Cache write price ratio"]) == caps.cache_write_price_ratio
    assert (
        _as_bool(doc["JSON response format"])
        == caps.supports_response_format_json
    )

    documented_cheap: Optional[str] = (
        None
        if doc["Cheap summariser model"] == "—"
        else _unticked(doc["Cheap summariser model"])
    )
    assert documented_cheap == caps.cheap_model_for_summariser


def test_documented_bedrock_prefixes_match_live_routing() -> None:
    tables = _parse_pipe_tables(_DOC_PATH.read_text(encoding="utf-8"))
    prefix_table = next(
        (t for t in tables if t[0][0] == "Model id prefix"), None
    )
    assert prefix_table is not None, (
        f"bedrock prefix table missing from {_DOC_PATH}"
    )

    documented: dict[str, set[str]] = {}
    for prefixes_cell, column_cell in prefix_table[1:]:
        for prefix in re.findall(r"`([^`]+)`", prefixes_cell):
            documented.setdefault(column_cell, set()).add(prefix)

    live_anthropic = {
        p for p, c in _BEDROCK_MODEL_CAPS.items() if c is _ANTHROPIC_FAMILY_CAPS
    }
    live_other = {
        p for p, c in _BEDROCK_MODEL_CAPS.items() if c is _NO_CACHE_FAMILY_CAPS
    }
    assert documented["Bedrock (Anthropic models)"] == live_anthropic
    assert documented["Bedrock (other families)"] == live_other
    # Every live prefix must be documented in one of the two rows.
    assert live_anthropic | live_other == set(_BEDROCK_MODEL_CAPS)
