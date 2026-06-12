"""Gemini provider tests.

Three surfaces under test:

* ``extract_usage`` — usage block extraction across the SDK
  attribute shape, the dict fixture shape, and camelCase REST keys.
* ``GeminiProvider.map_usage`` — the billing-shape contract:
  inclusive ``prompt_token_count``, thinking folded into output and
  surfaced as reasoning, cached counts always mapped to reads (the
  translator owns creation re-attribution), garbage tolerance.
* ``GeminiCacheManager`` — fingerprint stability, create-once /
  reuse-after, failure memoisation, and the SDK-missing degrade.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from inkfoot.providers.gemini import (
    GeminiCacheManager,
    GeminiProvider,
    cache_status_from_usage,
    extract_usage,
)
from tests.unit._fake_sdks import install_fake_gemini, uninstall_fake_sdks


@pytest.fixture(autouse=True)
def _no_fake_sdk_leak():
    uninstall_fake_sdks()
    yield
    uninstall_fake_sdks()


# ----------------------------------------------------------------------
# extract_usage
# ----------------------------------------------------------------------


def test_extract_usage_of_none_is_empty() -> None:
    assert extract_usage(None) == {}


def test_extract_usage_of_response_without_usage_is_empty() -> None:
    assert extract_usage({}) == {}
    assert extract_usage({"usage_metadata": None}) == {}
    assert extract_usage(SimpleNamespace()) == {}


def test_extract_usage_reads_snake_case_dict() -> None:
    usage = extract_usage(
        {
            "usage_metadata": {
                "prompt_token_count": 100,
                "candidates_token_count": 7,
                "cached_content_token_count": 60,
                "thoughts_token_count": 3,
            }
        }
    )
    assert usage == {
        "prompt_token_count": 100,
        "candidates_token_count": 7,
        "cached_content_token_count": 60,
        "thoughts_token_count": 3,
    }


def test_extract_usage_reads_camel_case_rest_shape() -> None:
    usage = extract_usage(
        {
            "usageMetadata": {
                "promptTokenCount": 100,
                "candidatesTokenCount": 7,
                "cachedContentTokenCount": 60,
            }
        }
    )
    assert usage["prompt_token_count"] == 100
    assert usage["candidates_token_count"] == 7
    assert usage["cached_content_token_count"] == 60


def test_extract_usage_reads_sdk_object_shape() -> None:
    response = SimpleNamespace(
        usage_metadata=SimpleNamespace(
            prompt_token_count=50,
            candidates_token_count=4,
            cached_content_token_count=0,
            thoughts_token_count=0,
        )
    )
    usage = extract_usage(response)
    assert usage["prompt_token_count"] == 50
    assert usage["candidates_token_count"] == 4


# ----------------------------------------------------------------------
# cache_status_from_usage
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "cached,expected", [(0, "n/a"), (1, "hit"), (4096, "hit")]
)
def test_cache_status_hit_only_when_cached_tokens_present(
    cached: int, expected: str
) -> None:
    status = cache_status_from_usage(
        {"cached_content_token_count": cached}
    )
    assert status == expected


# ----------------------------------------------------------------------
# map_usage
# ----------------------------------------------------------------------


def test_map_usage_prompt_count_is_inclusive_of_cached_portion() -> None:
    usage = GeminiProvider().map_usage(
        {
            "usage_metadata": {
                "prompt_token_count": 100,
                "candidates_token_count": 5,
                "cached_content_token_count": 60,
            }
        }
    )
    # Gemini's prompt_token_count already includes the cached part —
    # no overlay addition (unlike Anthropic).
    assert usage.input_tokens == 100
    assert usage.cache_read_tokens == 60
    assert usage.cache_status == "hit"


def test_map_usage_folds_thoughts_into_output_and_reasoning() -> None:
    usage = GeminiProvider().map_usage(
        {
            "usage_metadata": {
                "prompt_token_count": 10,
                "candidates_token_count": 50,
                "thoughts_token_count": 33,
            }
        }
    )
    # candidates excludes thinking; both bill at the output rate.
    assert usage.output_tokens == 83
    assert usage.reasoning_tokens == 33


def test_map_usage_cache_creation_is_always_zero() -> None:
    """The provider never attributes a write — Gemini's cache write
    happens at resource creation, which the translator re-attributes
    using the run-state marker."""
    usage = GeminiProvider().map_usage(
        {
            "usage_metadata": {
                "prompt_token_count": 100,
                "candidates_token_count": 5,
                "cached_content_token_count": 60,
            }
        }
    )
    assert usage.cache_creation_tokens == 0


def test_map_usage_no_cached_tokens_is_na() -> None:
    usage = GeminiProvider().map_usage(
        {
            "usage_metadata": {
                "prompt_token_count": 10,
                "candidates_token_count": 5,
            }
        }
    )
    assert usage.cache_read_tokens == 0
    assert usage.cache_status == "n/a"


def test_map_usage_sdk_object_shape_is_supported() -> None:
    response = SimpleNamespace(
        usage_metadata=SimpleNamespace(
            prompt_token_count=70,
            candidates_token_count=9,
            cached_content_token_count=30,
            thoughts_token_count=0,
        )
    )
    usage = GeminiProvider().map_usage(response)
    assert usage.input_tokens == 70
    assert usage.output_tokens == 9
    assert usage.cache_read_tokens == 30
    assert usage.cache_status == "hit"


def test_map_usage_never_raises_on_garbage_counters() -> None:
    from inkfoot.providers import TokenUsage

    response = {
        "usage_metadata": {
            "prompt_token_count": None,
            "candidates_token_count": "garbage",
            "cached_content_token_count": {"weird": 1},
            "thoughts_token_count": -3,
        }
    }
    assert GeminiProvider().map_usage(response) == TokenUsage()


# ----------------------------------------------------------------------
# GeminiCacheManager — fingerprint
# ----------------------------------------------------------------------


def test_fingerprint_is_stable_for_equal_inputs() -> None:
    a = GeminiCacheManager.fingerprint(
        "gemini-1.5-pro", "sys", [{"function_declarations": []}]
    )
    b = GeminiCacheManager.fingerprint(
        "gemini-1.5-pro", "sys", [{"function_declarations": []}]
    )
    assert a == b


@pytest.mark.parametrize(
    "other",
    [
        ("gemini-1.5-flash", "sys", None),
        ("gemini-1.5-pro", "different sys", None),
        ("gemini-1.5-pro", "sys", [{"function_declarations": []}]),
    ],
    ids=["model", "system", "tools"],
)
def test_fingerprint_changes_with_any_component(other: tuple) -> None:
    base = GeminiCacheManager.fingerprint("gemini-1.5-pro", "sys", None)
    assert GeminiCacheManager.fingerprint(*other) != base


def test_fingerprint_tolerates_non_serialisable_tools() -> None:
    class _Exotic:
        pass

    digest = GeminiCacheManager.fingerprint(
        "gemini-1.5-pro", "sys", [_Exotic()]
    )
    assert isinstance(digest, str) and len(digest) == 64


# ----------------------------------------------------------------------
# GeminiCacheManager — get_or_create
# ----------------------------------------------------------------------


def test_get_or_create_creates_once_then_reuses() -> None:
    fakes = install_fake_gemini()
    manager = GeminiCacheManager()

    first, created_first = manager.get_or_create(
        model="gemini-1.5-pro", system_instruction="sys"
    )
    second, created_second = manager.get_or_create(
        model="gemini-1.5-pro", system_instruction="sys"
    )

    assert created_first is True
    assert created_second is False
    assert first is second
    assert first.name.startswith("cachedContents/")
    assert len(fakes["cache_creations"]) == 1


def test_get_or_create_distinct_prefixes_create_distinct_resources() -> None:
    fakes = install_fake_gemini()
    manager = GeminiCacheManager()

    a, _ = manager.get_or_create(
        model="gemini-1.5-pro", system_instruction="sys-a"
    )
    b, _ = manager.get_or_create(
        model="gemini-1.5-pro", system_instruction="sys-b"
    )

    assert a is not b
    assert len(fakes["cache_creations"]) == 2


def test_get_or_create_omits_absent_optional_kwargs() -> None:
    fakes = install_fake_gemini()
    manager = GeminiCacheManager()

    manager.get_or_create(model="gemini-1.5-pro", system_instruction="sys")

    creation = fakes["cache_creations"][0]
    assert creation["model"] == "gemini-1.5-pro"
    assert creation["kwargs"] == {"system_instruction": "sys"}


def test_get_or_create_passes_ttl_through() -> None:
    fakes = install_fake_gemini()
    manager = GeminiCacheManager()

    manager.get_or_create(
        model="gemini-1.5-pro", system_instruction="sys", ttl="3600s"
    )

    assert fakes["cache_creations"][0]["kwargs"]["ttl"] == "3600s"


def test_creation_failure_degrades_and_is_memoised() -> None:
    fakes = install_fake_gemini()
    attempts = {"n": 0}

    def _boom(cls, model=None, **kwargs):
        attempts["n"] += 1
        raise RuntimeError("quota exceeded")

    fakes["CachedContent"].create = classmethod(_boom)
    manager = GeminiCacheManager()

    assert manager.get_or_create(
        model="gemini-1.5-pro", system_instruction="sys"
    ) == (None, False)
    assert manager.get_or_create(
        model="gemini-1.5-pro", system_instruction="sys"
    ) == (None, False)
    # The doomed network call ran exactly once — failures memoise.
    assert attempts["n"] == 1


def test_missing_sdk_degrades_to_none_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A None entry in sys.modules makes the import raise — the
    # deterministic stand-in for "google-generativeai not installed".
    monkeypatch.setitem(sys.modules, "google.generativeai", None)
    manager = GeminiCacheManager()
    assert manager.get_or_create(
        model="gemini-1.5-pro", system_instruction="sys"
    ) == (None, False)


def test_reset_drops_memoised_resources_and_failures() -> None:
    fakes = install_fake_gemini()
    manager = GeminiCacheManager()

    manager.get_or_create(model="gemini-1.5-pro", system_instruction="sys")
    manager.reset()
    _, created = manager.get_or_create(
        model="gemini-1.5-pro", system_instruction="sys"
    )

    assert created is True
    assert len(fakes["cache_creations"]) == 2
