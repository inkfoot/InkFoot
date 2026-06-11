"""``CheapSummariser`` — compress oversized tool results before they
compound.

Oversized tool results live forever in conversation history: every
subsequent turn resends them. This policy scans the outgoing request
for tool-result content above ``threshold_tokens`` and rewrites it in
place with a compressed version produced by the *same provider's*
cheap model (per :mod:`inkfoot.policy._cheap_models`), falling back
to mechanical truncation when the provider declares no cheap model or
the helper call fails.

Mechanics worth knowing:

* **Re-entrancy guard.** The summariser's own cheap-model call passes
  back through the shim — and therefore through this policy. A
  context variable marks the sub-call so ``before_call`` skips it
  (no recursion, no double-summarising) and stamps the call's
  metadata so the emit path attributes its tokens to
  ``ledger.summariser_tokens``.
* **Idempotence.** Summaries are cached by content hash. A raw result
  that reappears on later turns (conversation history) is swapped for
  the cached summary without a second cheap-model call, and the
  ``summariser_replaced`` event fires once per unique result, not
  once per turn.
* **Replay preservation.** With ``preserve_for_replay=True`` (the
  default) the raw result rides in the ``summariser_replaced``
  event's payload, so the model sees only the summary while the event
  log keeps the original.
* **Latency.** The cheap-model call is a real network round-trip and
  is deliberately *outside* the microsecond-scale hook budget the
  observation policies adhere to — it fires once per oversized
  result, then the cache absorbs repeats.

**A/B trust mode.** ``ab_mode=True`` holds out a control population:
with probability ``ab_sample_rate`` a run keeps its raw tool results
(branch A), otherwise it is summarised as usual (branch B). Each
sampled run carries a ``summariser_ab_assignment`` event so the
per-task populations can be compared offline (see
:mod:`inkfoot.policy._ab_pairing`). At the first summarisation
trigger of each run the policy re-evaluates the task's A/B history;
when the summarised branch's success rate trails the control branch
by more than ``regression_threshold`` (default five percentage
points), it emits a ``summariser_quality_regression`` event and
auto-disables itself for that task.

**Kill-switch.** ``inkfoot.tag("disable_summariser", True)`` inside a
run disables the policy for that run's task (process-wide, until
re-enabled); :func:`disable_summariser_for_task` does the same
programmatically.

Pattern C only: rewriting conversation state is only reliable when a
framework adapter re-supplies the messages each turn.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import random
import threading
from typing import TYPE_CHECKING, Any, Callable, Iterator, Optional

from inkfoot.policy import IntegrationPattern, Policy, PolicyDecision
from inkfoot.policy._ab_pairing import (
    ABObservation,
    CONTROL_BRANCH,
    TREATMENT_BRANCH,
    compute_quality_delta,
)
from inkfoot.policy._cheap_models import cheap_model_for
from inkfoot.policy._events import emit_policy_event
from inkfoot.shims._emit import SUMMARISER_CALL_METADATA_KEY
from inkfoot.tokenisers import tokenise

if TYPE_CHECKING:  # pragma: no cover
    from inkfoot.policy import CallContext
    from inkfoot.storage import Storage

_LOG = logging.getLogger("inkfoot.policy")

# ``inkfoot.tag`` key that opts the current run's task out of
# summarisation.
KILL_SWITCH_TAG = "disable_summariser"

# Set while the summariser's own cheap-model call is in flight, so
# the policy recognises (and skips) its own sub-call when the shim
# dispatches it back through before_call. ContextVar rather than a
# thread-local so async SDK paths propagate it correctly.
_in_summariser_call: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "inkfoot_in_summariser_call", default=False
)

_SUMMARISER_PROMPT = (
    "Condense the tool output below. Keep every fact, identifier, number, "
    "and error message that could matter for the user's question; drop "
    "boilerplate and repetition. Reply with only the condensed text.\n\n"
    "User's question:\n{question}\n\nTool output:\n{result}"
)

_TRUNCATION_MARKER = "\n[truncated by inkfoot]"

# How many historical runs the per-task regression check reads.
_AB_HISTORY_LIMIT = 200


# ----------------------------------------------------------------------
# Process-wide per-task kill switch
# ----------------------------------------------------------------------

# Shared across every CheapSummariser instance so the quality-
# regression auto-disable and the user-tag kill switch take effect
# regardless of which instance observed them.
_disabled_tasks: set[str] = set()
_disabled_lock = threading.Lock()


def disable_summariser_for_task(task: str) -> None:
    """Disable summarisation for ``task`` process-wide."""
    with _disabled_lock:
        _disabled_tasks.add(task)


def enable_summariser_for_task(task: str) -> None:
    """Re-enable summarisation for ``task`` (operator override after
    reviewing a regression). Idempotent."""
    with _disabled_lock:
        _disabled_tasks.discard(task)


def summariser_disabled_for_task(task: str) -> bool:
    with _disabled_lock:
        return task in _disabled_tasks


def _clear_disabled_tasks() -> None:
    """Tests only."""
    with _disabled_lock:
        _disabled_tasks.clear()


# ----------------------------------------------------------------------
# Request scanning
# ----------------------------------------------------------------------


def _tool_result_text(block: dict[str, Any]) -> Optional[str]:
    """Text of one Anthropic ``tool_result`` block. Returns ``None``
    for shapes we must not rewrite (e.g. content carrying image
    blocks) — replacing those with a string would silently drop
    non-text payload."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for sub in content:
            if not (isinstance(sub, dict) and sub.get("type") == "text"):
                return None
            txt = sub.get("text", "")
            if not isinstance(txt, str):
                return None
            parts.append(txt)
        return "".join(parts)
    return None


def _iter_tool_result_slots(
    request_kwargs: dict[str, Any],
) -> Iterator[tuple[dict[str, Any], str, Optional[str]]]:
    """Yield ``(container, text, tool_id)`` for every rewritable
    tool result in the request: Anthropic ``tool_result`` content
    blocks and OpenAI ``role="tool"`` messages. The container is the
    dict whose ``"content"`` key holds the raw text."""
    for msg in request_kwargs.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "tool":  # OpenAI shape
            content = msg.get("content")
            if isinstance(content, str) and content:
                yield msg, content, msg.get("tool_call_id")
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                text = _tool_result_text(block)
                if text:
                    yield block, text, block.get("tool_use_id")


def _last_user_question(request_kwargs: dict[str, Any]) -> str:
    """Most recent user message that carries actual text (skipping
    tool-result blocks, which ride on user-role messages in the
    Anthropic API). Fed to the summariser prompt so the summary keeps
    what the question needs."""
    question = ""
    for msg in request_kwargs.get("messages") or []:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            if content.strip():
                question = content
        elif isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    txt = block.get("text", "")
                    if isinstance(txt, str):
                        parts.append(txt)
            joined = " ".join(parts).strip()
            if joined:
                question = joined
    return question


def _truncate_to_tokens(text: str, max_tokens: int, model: str) -> str:
    """Mechanical fallback: first ``max_tokens`` tokens + marker,
    sized so the marker fits inside the budget.

    The cut is tiktoken-guided, but the budget is enforced against
    ``model``'s own tokeniser — the recorded ``summary_tokens`` uses
    that count, so a cut sized in tiktoken alone could overrun it
    when the two disagree (e.g. Anthropic models)."""
    import tiktoken  # noqa: PLC0415

    encoding = tiktoken.get_encoding("o200k_base")
    tokens = encoding.encode(text)
    if len(tokens) <= max_tokens and tokenise(text, model).value <= max_tokens:
        return text
    marker_len = len(encoding.encode(_TRUNCATION_MARKER))
    head = max(1, min(len(tokens), max_tokens - marker_len))
    candidate = encoding.decode(tokens[:head]) + _TRUNCATION_MARKER
    while head > 1:
        measured = tokenise(candidate, model).value
        if measured <= max_tokens:
            break
        # Shrink proportionally to the overshoot, strictly decreasing
        # so the loop always terminates (head == 1 is the floor).
        head = max(1, min(head - 1, head * max_tokens // measured))
        candidate = encoding.decode(tokens[:head]) + _TRUNCATION_MARKER
    return candidate


def _storage() -> Optional["Storage"]:
    try:
        from inkfoot._instrument import _STORAGE  # noqa: PLC0415

        return _STORAGE
    except Exception:  # pylint: disable=broad-except  # pragma: no cover
        return None


def _task_for_run(run_id: str) -> Optional[str]:
    storage = _storage()
    if storage is None or not hasattr(storage, "get_run"):
        return None
    try:
        row = storage.get_run(run_id)
    except Exception:  # pylint: disable=broad-except
        return None
    if not row:
        return None
    task = row.get("task")
    return task if isinstance(task, str) and task else None


def _gather_ab_observations(
    storage: "Storage", task: str, *, limit: int = _AB_HISTORY_LIMIT
) -> list[ABObservation]:
    """Read the task's completed A/B-assigned runs from storage.

    SQLite-specific today (mirrors the contract runtime's projection
    reads); other backends return an empty list, which simply means
    the regression check stays silent there.
    """
    try:
        conn = storage._conn()  # type: ignore[attr-defined]  # noqa: SLF001
    except Exception:  # pylint: disable=broad-except
        return []
    try:
        cur = conn.execute(
            """
            SELECT r.id, r.outcome, r.quality_score, e.payload_json
            FROM runs r JOIN events e ON e.run_id = r.id
            WHERE r.task = ?
              AND e.kind = 'summariser_ab_assignment'
              AND r.outcome IS NOT NULL
            ORDER BY r.started_at DESC
            LIMIT ?
            """,
            [task, int(limit)],
        )
        rows = cur.fetchall()
    except Exception:  # pylint: disable=broad-except
        return []

    observations: list[ABObservation] = []
    for run_id, outcome, quality_score, payload_json in rows:
        try:
            payload = json.loads(payload_json or "{}")
        except (TypeError, ValueError):
            continue
        branch = payload.get("branch")
        if branch not in (CONTROL_BRANCH, TREATMENT_BRANCH):
            continue
        observations.append(
            ABObservation(
                run_id=str(run_id),
                task=task,
                branch=branch,
                outcome=outcome,
                quality_score=(
                    float(quality_score)
                    if isinstance(quality_score, (int, float))
                    else None
                ),
            )
        )
    return observations


# ----------------------------------------------------------------------
# The policy
# ----------------------------------------------------------------------


class CheapSummariser(Policy):
    """Replace oversized tool results with cheap-model summaries
    (framework adapters only).

    ``threshold_tokens`` — tool results at or below this size pass
    through unchanged. ``max_summary_tokens`` — hard ceiling on the
    replacement text (model output that overruns is truncated).
    ``preserve_for_replay`` — keep the raw result in the
    ``summariser_replaced`` event payload. ``ab_mode`` /
    ``ab_sample_rate`` — opt-in trust mode, see the module docstring.
    ``rng`` — injectable ``() -> float`` for deterministic branch
    assignment in tests; defaults to :func:`random.random`.
    """

    NAME = "CheapSummariser"
    SUPPORTED_PATTERNS = {IntegrationPattern.C}

    def __init__(
        self,
        *,
        threshold_tokens: int = 1500,
        max_summary_tokens: int = 600,
        preserve_for_replay: bool = True,
        ab_mode: bool = False,
        ab_sample_rate: float = 0.10,
        regression_threshold: float = 0.05,
        regression_min_runs: int = 5,
        rng: Optional[Callable[[], float]] = None,
    ) -> None:
        if threshold_tokens < 1:
            raise ValueError(
                f"CheapSummariser: threshold_tokens must be >= 1, "
                f"got {threshold_tokens}"
            )
        if max_summary_tokens < 1:
            raise ValueError(
                f"CheapSummariser: max_summary_tokens must be >= 1, "
                f"got {max_summary_tokens}"
            )
        if not 0.0 <= ab_sample_rate <= 1.0:
            raise ValueError(
                f"CheapSummariser: ab_sample_rate must be in [0, 1], "
                f"got {ab_sample_rate}"
            )
        if not 0.0 <= regression_threshold <= 1.0:
            raise ValueError(
                f"CheapSummariser: regression_threshold must be in [0, 1], "
                f"got {regression_threshold}"
            )
        if regression_min_runs < 1:
            raise ValueError(
                f"CheapSummariser: regression_min_runs must be >= 1, "
                f"got {regression_min_runs}"
            )
        self._threshold_tokens = threshold_tokens
        self._max_summary_tokens = max_summary_tokens
        self._preserve_for_replay = preserve_for_replay
        self._ab_mode = ab_mode
        self._ab_sample_rate = ab_sample_rate
        self._regression_threshold = regression_threshold
        self._regression_min_runs = regression_min_runs
        self._rng = rng or random.random

        self._lock = threading.Lock()
        self._summary_cache: dict[str, str] = {}  # sha256(raw) -> summary
        self._ab_branch: dict[str, str] = {}  # run_id -> "A" | "B"
        self._tag_checked: set[str] = set()  # run ids scanned for the kill tag
        self._regression_checked: set[str] = set()
        self._task_cache: dict[str, Optional[str]] = {}  # run_id -> task

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------

    def before_call(self, ctx: "CallContext") -> "PolicyDecision":
        if _in_summariser_call.get():
            # Our own helper call coming back through the shim: skip
            # all processing and stamp it so the emit path attributes
            # its tokens to ledger.summariser_tokens.
            ctx.metadata[SUMMARISER_CALL_METADATA_KEY] = True
            return PolicyDecision(action="allow")

        candidates = self._oversized_results(ctx)
        if not candidates:
            return PolicyDecision(action="allow")

        task = self._cached_task(ctx.run_id)
        self._check_kill_switch_tag(ctx.run_id, task)
        if task and summariser_disabled_for_task(task):
            return PolicyDecision(action="allow")

        if self._ab_mode and task:
            self._maybe_check_regression(ctx.run_id, task)
            if summariser_disabled_for_task(task):
                return PolicyDecision(action="allow")
            if self._assign_branch(ctx.run_id, task) == CONTROL_BRANCH:
                # Control population: raw results stay.
                return PolicyDecision(action="allow")

        for container, text, tokens, tool_id in candidates:
            self._replace(ctx, container, text, tokens, tool_id)
        return PolicyDecision(action="allow")

    def after_call(self, ctx: "CallContext", response: Any) -> None:
        return None

    def _cached_task(self, run_id: str) -> Optional[str]:
        """Per-run memo over :func:`_task_for_run` — a run's task never
        changes, so storage is read once per run, not on every
        triggering call."""
        with self._lock:
            if run_id in self._task_cache:
                return self._task_cache[run_id]
        task = _task_for_run(run_id)
        with self._lock:
            self._task_cache[run_id] = task
        return task

    # ------------------------------------------------------------------
    # Scanning + replacement
    # ------------------------------------------------------------------

    def _oversized_results(
        self, ctx: "CallContext"
    ) -> list[tuple[dict[str, Any], str, int, Optional[str]]]:
        out: list[tuple[dict[str, Any], str, int, Optional[str]]] = []
        for container, text, tool_id in _iter_tool_result_slots(
            ctx.request_kwargs
        ):
            # Fast path: one token is at least one character, so a
            # text shorter than the threshold in characters can never
            # exceed it in tokens. Skips the tokeniser on small (and
            # already-summarised) results.
            if len(text) <= self._threshold_tokens:
                continue
            tokens = tokenise(text, ctx.model).value
            if tokens > self._threshold_tokens:
                out.append((container, text, tokens, tool_id))
        return out

    def _replace(
        self,
        ctx: "CallContext",
        container: dict[str, Any],
        text: str,
        original_tokens: int,
        tool_id: Optional[str],
    ) -> None:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self._lock:
            cached = self._summary_cache.get(digest)
        if cached is not None:
            # Conversation history resent the same raw result; swap in
            # the known summary without another cheap-model call (and
            # without a duplicate event).
            container["content"] = cached
            return

        question = _last_user_question(ctx.request_kwargs)
        summary_model = cheap_model_for(ctx.provider)
        summary: Optional[str] = None
        if summary_model is not None:
            summary = self._call_cheap_model(
                ctx.provider, summary_model, text, question
            )
        used = summary_model if summary is not None else "truncation"
        if summary is None:
            summary = _truncate_to_tokens(text, self._max_summary_tokens, ctx.model)
        elif tokenise(summary, ctx.model).value > self._max_summary_tokens:
            # The cheap model overran max_tokens semantics or the
            # tokenisers disagree — enforce the ceiling mechanically.
            summary = _truncate_to_tokens(
                summary, self._max_summary_tokens, ctx.model
            )

        with self._lock:
            self._summary_cache[digest] = summary
        container["content"] = summary

        payload: dict[str, Any] = {
            "result_hash": digest,
            "original_tokens": original_tokens,
            "summary_tokens": tokenise(summary, ctx.model).value,
            "summariser_model": used,
            "tool_id": tool_id,
        }
        if self._preserve_for_replay:
            payload["raw"] = text
        emit_policy_event(ctx.run_id, "summariser_replaced", payload)

    def _call_cheap_model(
        self, provider: str, model: str, result_text: str, question: str
    ) -> Optional[str]:
        """One round-trip to the provider's cheap model. Returns the
        summary text, or ``None`` on any failure (the caller falls
        back to truncation). The re-entrancy guard is held for the
        duration so the nested shim dispatch recognises the call."""
        prompt = _SUMMARISER_PROMPT.format(
            question=question or "(not available)", result=result_text
        )
        token = _in_summariser_call.set(True)
        try:
            if provider == "anthropic":
                return _anthropic_summary(model, prompt, self._max_summary_tokens)
            if provider == "openai":
                return _openai_summary(model, prompt, self._max_summary_tokens)
            return None
        except Exception:  # pylint: disable=broad-except
            _LOG.warning(
                "CheapSummariser: cheap-model call failed; falling back to "
                "truncation",
                exc_info=True,
            )
            return None
        finally:
            _in_summariser_call.reset(token)

    # ------------------------------------------------------------------
    # Kill switch + A/B mode
    # ------------------------------------------------------------------

    def _check_kill_switch_tag(self, run_id: str, task: Optional[str]) -> None:
        """Once per run: scan the run's events for the
        ``disable_summariser`` user tag and, when found, disable the
        run's task. Scanned at the first summarisation trigger so the
        common ``tag(...)``-right-after-``agent_run(...)`` pattern is
        honoured within the same run."""
        with self._lock:
            if run_id in self._tag_checked:
                return
            self._tag_checked.add(run_id)
        if not task:
            return
        storage = _storage()
        if storage is None:
            return
        try:
            for ev in storage.iter_events(run_id):
                if ev.get("kind") != "user_tag":
                    continue
                try:
                    payload = json.loads(ev.get("payload_json") or "{}")
                except (TypeError, ValueError):
                    continue
                if payload.get("key") == KILL_SWITCH_TAG and payload.get("value"):
                    disable_summariser_for_task(task)
                    return
        except Exception:  # pylint: disable=broad-except
            _LOG.warning(
                "CheapSummariser: kill-switch tag scan failed for run %s",
                run_id,
                exc_info=True,
            )

    def _assign_branch(self, run_id: str, task: str) -> str:
        """Sticky per-run branch assignment. One run never mixes
        branches — outcomes are per-run, so a mixed run would be
        unpairable."""
        with self._lock:
            existing = self._ab_branch.get(run_id)
            if existing is not None:
                return existing
            branch = (
                CONTROL_BRANCH
                if self._rng() < self._ab_sample_rate
                else TREATMENT_BRANCH
            )
            self._ab_branch[run_id] = branch
        emit_policy_event(
            run_id,
            "summariser_ab_assignment",
            {"task": task, "branch": branch},
        )
        return branch

    def _maybe_check_regression(self, run_id: str, task: str) -> None:
        """Once per run: compare the task's A/B history and
        auto-disable when the summarised branch underperforms. Runs
        synchronously at the first trigger of a run, so a regression
        detected here takes effect for this run and every later run
        of the task."""
        with self._lock:
            if run_id in self._regression_checked:
                return
            self._regression_checked.add(run_id)
        storage = _storage()
        if storage is None:
            return
        observations = _gather_ab_observations(storage, task)
        delta = compute_quality_delta(
            task, observations, min_runs_per_branch=self._regression_min_runs
        )
        if delta is None or delta.success_rate_drop <= self._regression_threshold:
            return
        disable_summariser_for_task(task)
        emit_policy_event(
            run_id,
            "summariser_quality_regression",
            {
                "task": task,
                "control_runs": delta.control_runs,
                "treatment_runs": delta.treatment_runs,
                "control_success_rate": delta.control_success_rate,
                "treatment_success_rate": delta.treatment_success_rate,
                "success_rate_drop": delta.success_rate_drop,
                "quality_score_delta": delta.quality_score_delta,
                "threshold": self._regression_threshold,
            },
        )
        _LOG.warning(
            "CheapSummariser auto-disabled for task %r: summarised runs "
            "succeed %.1f%% vs %.1f%% raw (drop %.1fpp > %.1fpp threshold)",
            task,
            delta.treatment_success_rate * 100,
            delta.control_success_rate * 100,
            delta.success_rate_drop * 100,
            self._regression_threshold * 100,
        )

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    def reset(self) -> None:
        with self._lock:
            self._summary_cache.clear()
            self._ab_branch.clear()
            self._tag_checked.clear()
            self._regression_checked.clear()
            self._task_cache.clear()


# ----------------------------------------------------------------------
# Provider round-trips
# ----------------------------------------------------------------------


def _anthropic_summary(model: str, prompt: str, max_tokens: int) -> Optional[str]:
    import anthropic  # noqa: PLC0415

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    content = (
        response.get("content")
        if isinstance(response, dict)
        else getattr(response, "content", None)
    )
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
        elif getattr(block, "type", "") == "text":
            txt = getattr(block, "text", "")
            if isinstance(txt, str):
                parts.append(txt)
    text = "".join(parts).strip()
    return text or None


def _openai_summary(model: str, prompt: str, max_tokens: int) -> Optional[str]:
    import openai  # noqa: PLC0415

    client = openai.OpenAI()
    response = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    choices = (
        response.get("choices")
        if isinstance(response, dict)
        else getattr(response, "choices", None)
    )
    if not choices:
        return None
    first = choices[0]
    message = (
        first.get("message")
        if isinstance(first, dict)
        else getattr(first, "message", None)
    )
    content = (
        message.get("content")
        if isinstance(message, dict)
        else getattr(message, "content", None)
    )
    if isinstance(content, str):
        text = content.strip()
        return text or None
    return None
