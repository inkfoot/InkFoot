"""Redaction-layer tests.

Covers:
- The regex floor masks every default shape (email, the provider
  API-key prefixes, JWT) wherever it sits in a nested payload.
- Dictionary keys and non-string leaves are left alone.
- The audit log is counts-only and never carries the matched text.
- ``compose`` chains hooks left-to-right; the floor composes behind a
  custom hook so both fire.
- ``resolve_redaction_hook`` picks the right hook per capture mode.
- ``apply_to_content`` round-trips serialised content, flags whether
  anything changed, and fails closed on a misbehaving hook.
"""

from __future__ import annotations

import json
import logging

import pytest

from inkfoot.storage.redaction import (
    RedactionContext,
    RedactionHook,
    RegexRedactor,
    apply_to_content,
    compose,
    default_redactor,
    resolve_redaction_hook,
)


def _ctx(**over) -> RedactionContext:
    base = dict(
        run_id="run-1",
        event_id="evt-1",
        kind="llm_call",
        capture_mode="replay",
        sequence=1,
    )
    base.update(over)
    return RedactionContext(**base)


# Realistic-looking but synthetic secrets.
EMAIL = "alice.smith@example.com"
OPENAI_KEY = "sk-proj-ABCDEFGHIJKLMNOPqrstuvwx0123"
ANTHROPIC_KEY = "sk-ant-api03-ZYXWVUTSRQ9876543210"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.s1gNature_tok-EN"


# ----------------------------------------------------------------------
# Floor pattern coverage
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret, expected_name",
    [
        (EMAIL, "email"),
        (OPENAI_KEY, "openai_key"),
        (ANTHROPIC_KEY, "anthropic_key"),
        (JWT, "jwt"),
    ],
)
def test_floor_masks_each_default_shape(secret, expected_name) -> None:
    floor = default_redactor()
    out = floor({"request": f"value is {secret} here"}, _ctx())
    assert secret not in out["request"]
    assert f"[REDACTED:{expected_name}]" in out["request"]


def test_floor_scrubs_nested_structures() -> None:
    floor = default_redactor()
    payload = {
        "request": {
            "messages": [
                {"role": "user", "content": f"mail me at {EMAIL}"},
                {"role": "system", "content": [f"key {OPENAI_KEY}"]},
            ]
        }
    }
    out = floor(payload, _ctx())
    blob = json.dumps(out)
    assert EMAIL not in blob
    assert OPENAI_KEY not in blob
    assert "[REDACTED:email]" in blob
    assert "[REDACTED:openai_key]" in blob


def test_floor_does_not_mutate_input() -> None:
    floor = default_redactor()
    payload = {"request": {"content": f"mail {EMAIL}"}}
    original = json.dumps(payload)
    floor(payload, _ctx())
    assert json.dumps(payload) == original


def test_floor_leaves_dict_keys_untouched() -> None:
    # An email-shaped *key* is structural, not content; only values are
    # scrubbed so the payload shape is preserved.
    floor = default_redactor()
    out = floor({"request": {EMAIL: "hello"}}, _ctx())
    assert EMAIL in out["request"]


def test_floor_leaves_non_string_leaves_untouched() -> None:
    floor = default_redactor()
    payload = {"request": {"n": 5, "f": 1.5, "b": True, "z": None}}
    out = floor(payload, _ctx())
    assert out["request"] == {"n": 5, "f": 1.5, "b": True, "z": None}


def test_anthropic_key_counted_separately_from_openai_key() -> None:
    # ``sk-ant-`` is a prefix-superset of ``sk-``; the more specific
    # shape must win the attribution so counts stay honest.
    floor = default_redactor()
    counts: dict[str, int] = {}
    out = floor(
        {"request": f"{ANTHROPIC_KEY} and {OPENAI_KEY}"}, _ctx()
    )
    text = out["request"]
    assert "[REDACTED:anthropic_key]" in text
    assert "[REDACTED:openai_key]" in text
    assert ANTHROPIC_KEY not in text and OPENAI_KEY not in text


def test_multiple_hits_of_same_pattern_all_redacted() -> None:
    floor = default_redactor()
    out = floor(
        {"request": f"{EMAIL} then bob@elsewhere.org"}, _ctx()
    )
    assert "@" not in out["request"].replace("[REDACTED:email]", "")


def test_custom_placeholder_is_honoured() -> None:
    redactor = RegexRedactor(placeholder="<{name}>")
    out = redactor({"request": f"mail {EMAIL}"}, _ctx())
    assert "<email>" in out["request"]
    assert EMAIL not in out["request"]


# ----------------------------------------------------------------------
# Audit log — counts only, never the matched text
# ----------------------------------------------------------------------


def test_audit_log_records_counts_never_text(caplog) -> None:
    floor = default_redactor()
    with caplog.at_level(logging.INFO, logger="inkfoot.redaction"):
        floor(
            {
                "request": f"{EMAIL} {EMAIL}",
                "response": f"{JWT}",
            },
            _ctx(),
        )
    records = [r for r in caplog.records if "redaction_audit" in r.getMessage()]
    assert len(records) == 1
    record = records[0]
    # Counts are exposed both structurally and in the message.
    assert record.redaction_counts == {"email": 2, "jwt": 1}
    assert "email=2" in record.getMessage()
    # The secret itself must never appear anywhere in the record.
    assert EMAIL not in record.getMessage()
    assert JWT not in record.getMessage()
    assert all(EMAIL not in str(a) for a in record.args)


def test_no_audit_log_when_nothing_matched(caplog) -> None:
    floor = default_redactor()
    with caplog.at_level(logging.INFO, logger="inkfoot.redaction"):
        floor({"request": "nothing sensitive here"}, _ctx())
    assert not [
        r for r in caplog.records if "redaction_audit" in r.getMessage()
    ]


# ----------------------------------------------------------------------
# compose
# ----------------------------------------------------------------------


def test_compose_runs_hooks_left_to_right() -> None:
    order: list[str] = []

    def first(payload, ctx):
        order.append("first")
        return {**payload, "request": payload["request"] + "-A"}

    def second(payload, ctx):
        order.append("second")
        return {**payload, "request": payload["request"] + "-B"}

    composed = compose(first, second)
    out = composed({"request": "x"}, _ctx())
    assert order == ["first", "second"]
    assert out["request"] == "x-A-B"


def test_compose_skips_none_and_is_identity_when_empty() -> None:
    identity = compose(None, None)
    payload = {"request": "unchanged"}
    assert identity(payload, _ctx()) == payload


def test_compose_single_hook_returns_it_unwrapped() -> None:
    floor = default_redactor()
    assert compose(floor) is floor


# ----------------------------------------------------------------------
# resolve_redaction_hook
# ----------------------------------------------------------------------


def test_resolve_metadata_mode_is_none_even_with_custom() -> None:
    assert resolve_redaction_hook("metadata", default_redactor()) is None


def test_resolve_replay_without_custom_is_the_floor() -> None:
    hook = resolve_redaction_hook("replay", None)
    out = hook({"request": f"mail {EMAIL}"}, _ctx())
    assert EMAIL not in out["request"]


def test_resolve_replay_with_custom_runs_both_floor_and_custom() -> None:
    def org_hook(payload, ctx):
        return {
            k: (v.replace("ORG-123", "[ORG]") if isinstance(v, str) else v)
            for k, v in payload.items()
        }

    hook = resolve_redaction_hook("replay", org_hook)
    out = hook({"request": f"token ORG-123 and {EMAIL}"}, _ctx())
    # Custom shape masked...
    assert "[ORG]" in out["request"]
    assert "ORG-123" not in out["request"]
    # ...and the floor still ran alongside it.
    assert EMAIL not in out["request"]
    assert "[REDACTED:email]" in out["request"]


# ----------------------------------------------------------------------
# apply_to_content — the storage-facing helper
# ----------------------------------------------------------------------


def test_apply_none_hook_passes_through_unchanged() -> None:
    req = '{"a": 1}'
    out = apply_to_content(
        None,
        ctx=_ctx(),
        request_json=req,
        response_json=None,
        tool_result_json=None,
    )
    assert out == (req, None, None, False)


def test_apply_redacts_and_flags_changed() -> None:
    floor = default_redactor()
    req = json.dumps({"system": f"mail {EMAIL}"})
    new_req, new_resp, new_tool, redacted = apply_to_content(
        floor,
        ctx=_ctx(),
        request_json=req,
        response_json=None,
        tool_result_json=None,
    )
    assert redacted is True
    assert EMAIL not in new_req
    assert new_resp is None and new_tool is None


def test_apply_clean_content_returns_original_strings() -> None:
    floor = default_redactor()
    req = '{"messages": ["hello world"]}'
    resp = '{"ok": true}'
    new_req, new_resp, new_tool, redacted = apply_to_content(
        floor,
        ctx=_ctx(),
        request_json=req,
        response_json=resp,
        tool_result_json=None,
    )
    assert redacted is False
    # Byte-identical pass-through when nothing changed (no churn).
    assert new_req == req
    assert new_resp == resp
    assert new_tool is None


def test_apply_absent_field_stays_none() -> None:
    floor = default_redactor()
    new_req, new_resp, new_tool, redacted = apply_to_content(
        floor,
        ctx=_ctx(),
        request_json=json.dumps({"x": f"mail {EMAIL}"}),
        response_json=None,
        tool_result_json=None,
    )
    assert new_resp is None
    assert new_tool is None
    assert redacted is True


def test_apply_unparseable_content_is_still_scrubbed() -> None:
    # Defensive: even a malformed body must not leak PII to disk.
    floor = default_redactor()
    bad = f"contact {EMAIL} {{ truncated"
    new_req, _, _, redacted = apply_to_content(
        floor,
        ctx=_ctx(),
        request_json=bad,
        response_json=None,
        tool_result_json=None,
    )
    assert redacted is True
    assert EMAIL not in new_req


def test_apply_fails_closed_when_hook_raises() -> None:
    def boom(payload, ctx):
        raise RuntimeError("hook bug")

    new_req, new_resp, new_tool, redacted = apply_to_content(
        boom,
        ctx=_ctx(),
        request_json=json.dumps({"system": f"mail {EMAIL}"}),
        response_json=None,
        tool_result_json=None,
    )
    # Content dropped rather than written unredacted.
    assert (new_req, new_resp, new_tool) == (None, None, None)
    assert redacted is True


def test_apply_fails_closed_when_hook_returns_non_mapping() -> None:
    def wrong(payload, ctx):
        return "not a mapping"

    out = apply_to_content(
        wrong,
        ctx=_ctx(),
        request_json=json.dumps({"system": f"mail {EMAIL}"}),
        response_json=None,
        tool_result_json=None,
    )
    assert out == (None, None, None, True)


def test_apply_hook_dropped_field_becomes_sql_null_not_json_null() -> None:
    # A hook that drops a body (returns None for a key it had content
    # for) must store SQL NULL, not the JSON literal "null".
    def drop_response(payload, ctx):
        return {
            "request": payload["request"],
            "response": None,
            "tool_result": payload["tool_result"],
        }

    new_req, new_resp, new_tool, redacted = apply_to_content(
        drop_response,
        ctx=_ctx(),
        request_json=json.dumps({"a": 1}),
        response_json=json.dumps({"secret": "value"}),
        tool_result_json=None,
    )
    assert redacted is True
    # A true NULL — not the 4-byte string "null".
    assert new_resp is None
    assert json.loads(new_req) == {"a": 1}
    assert new_tool is None


# ----------------------------------------------------------------------
# Types
# ----------------------------------------------------------------------


def test_redaction_context_is_frozen() -> None:
    ctx = _ctx()
    with pytest.raises(Exception):
        ctx.run_id = "mutated"  # type: ignore[misc]


def test_floor_satisfies_redaction_hook_protocol() -> None:
    assert isinstance(default_redactor(), RedactionHook)
