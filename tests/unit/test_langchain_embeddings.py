"""LangChain embeddings capture tests.

LangChain has no embeddings callback, so capture is done by patching
the embedding methods on ``langchain_core.embeddings.Embeddings``
subclasses (``inkfoot.langchain.embeddings``). These tests drive
*real* ``Embeddings`` subclasses end to end — instantiate, call
``embed_documents`` / ``embed_query``, and assert an ``embedding_call``
event lands — which is the behaviour the framework actually exercises.
Providers without a raw-SDK embeddings shim (Gemini, Bedrock, Voyage)
are the whole point of this path.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("langchain_core", reason="langchain-core not installed")

from langchain_core.embeddings import Embeddings

import inkfoot
import inkfoot._instrument as instrument_mod
from inkfoot._run_context import _clear_current_run, _set_current_run
from inkfoot.langchain import InkfootCallbackHandler
from inkfoot.policy.registry import PolicyRegistry
from inkfoot.storage.sqlite import SQLiteStorage
from tests.unit._fake_sdks import install_fake_openai, uninstall_fake_sdks


# Module-level subclasses — created at import, so they exist when the
# install-time subclass walk runs. Class names sniff to providers.
class VoyageAIEmbeddings(Embeddings):
    model = "voyage-3"

    def embed_documents(self, texts):
        return [[0.1, 0.2] for _ in texts]

    def embed_query(self, text):
        return [0.1, 0.2]


class BedrockEmbeddings(Embeddings):
    model_id = "amazon.titan-embed-text-v2:0"

    def embed_documents(self, texts):
        return [[0.0] for _ in texts]

    def embed_query(self, text):
        return [0.0]


class GoogleGenerativeAIEmbeddings(Embeddings):
    model = "models/text-embedding-004"

    def embed_documents(self, texts):
        return [[0.0] for _ in texts]

    def embed_query(self, text):
        return [0.0]


class NativeAsyncEmbeddings(Embeddings):
    """Overrides the async methods natively (doesn't delegate to sync)."""

    model = "voyage-3"

    def embed_documents(self, texts):
        return [[0.0] for _ in texts]

    def embed_query(self, text):
        return [0.0]

    async def aembed_documents(self, texts):
        return [[0.0] for _ in texts]

    async def aembed_query(self, text):
        return [0.0]


@pytest.fixture(autouse=True)
def clean_state():
    instrument_mod.shutdown()
    PolicyRegistry.clear()
    _clear_current_run()
    uninstall_fake_sdks()
    yield
    instrument_mod.shutdown()
    PolicyRegistry.clear()
    _clear_current_run()
    uninstall_fake_sdks()


def _boot(tmp_path, *, sdks=None) -> SQLiteStorage:
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    # langchain="auto" (the default) keeps the LangChain embeddings shim
    # active — it's gated on the langchain setting, not just embeddings.
    inkfoot.instrument(
        storage=storage, sdks=sdks if sdks is not None else [],
        embeddings=True,
    )
    storage.start_run(
        run_id="emb-run", task="t", agent_kind="u", started_at=1_700_000_000_000
    )
    _set_current_run("emb-run")
    return storage


def _events(storage: SQLiteStorage) -> list[dict]:
    return [
        json.loads(ev["payload_json"])
        for ev in storage.iter_events("emb-run")
        if ev["kind"] == "embedding_call"
    ]


# ----------------------------------------------------------------------
# End-to-end capture of a shim-less provider
# ----------------------------------------------------------------------


def test_embed_documents_lands_one_event(tmp_path) -> None:
    storage = _boot(tmp_path)
    VoyageAIEmbeddings().embed_documents(["hello world", "second chunk"])
    events = _events(storage)
    assert len(events) == 1
    assert events[0]["provider"] == "voyage"
    assert events[0]["model"] == "voyage-3"
    assert events[0]["batch_size"] == 2
    assert events[0]["input_tokens"] > 0
    assert events[0]["token_count_estimated"] is True


def test_embed_query_lands_one_event_batch_size_one(tmp_path) -> None:
    storage = _boot(tmp_path)
    VoyageAIEmbeddings().embed_query("a query")
    events = _events(storage)
    assert len(events) == 1
    assert events[0]["batch_size"] == 1


@pytest.mark.parametrize(
    "cls, expected_provider",
    [
        (VoyageAIEmbeddings, "voyage"),
        (BedrockEmbeddings, "bedrock"),
        (GoogleGenerativeAIEmbeddings, "gemini"),
    ],
)
def test_provider_resolved_from_class_name(
    tmp_path, cls, expected_provider
) -> None:
    storage = _boot(tmp_path)
    cls().embed_query("x")
    assert _events(storage)[0]["provider"] == expected_provider


def test_model_id_attribute_is_resolved(tmp_path) -> None:
    """Bedrock names its model attr ``model_id``, not ``model``."""
    storage = _boot(tmp_path)
    BedrockEmbeddings().embed_query("x")
    assert _events(storage)[0]["model"] == "amazon.titan-embed-text-v2:0"


def test_gemini_models_prefix_stripped_so_cost_resolves(tmp_path) -> None:
    """Google's SDK reports ``models/<name>``; the prefix is stripped to
    the bare pricing key so a paid Gemini embedding model resolves a
    cost instead of falling through to ``(unpriced)``."""
    storage = _boot(tmp_path)

    class GoogleGenerativeAIPaidEmbeddings(Embeddings):
        model = "models/gemini-embedding-001"

        def embed_documents(self, texts):
            return [[0.0] for _ in texts]

        def embed_query(self, text):
            return [0.0]

    GoogleGenerativeAIPaidEmbeddings().embed_query("hello world tokens here")
    event = _events(storage)[0]
    assert event["provider"] == "gemini"
    assert event["model"] == "gemini-embedding-001"  # prefix stripped
    # gemini-embedding-001 lists at $0.15/Mtok = 150 nd/token.
    assert event["estimated_nanodollars"] == event["input_tokens"] * 150


# ----------------------------------------------------------------------
# Async
# ----------------------------------------------------------------------


def test_async_default_delegates_and_captures_once(tmp_path) -> None:
    """A subclass that overrides only the sync methods inherits the
    base async default, which routes through the wrapped sync method —
    so exactly one event, not zero or two."""
    storage = _boot(tmp_path)
    asyncio.run(VoyageAIEmbeddings().aembed_documents(["a", "b"]))
    assert len(_events(storage)) == 1
    assert _events(storage)[0]["batch_size"] == 2


def test_native_async_methods_are_captured(tmp_path) -> None:
    storage = _boot(tmp_path)
    asyncio.run(NativeAsyncEmbeddings().aembed_documents(["a", "b", "c"]))
    events = _events(storage)
    assert len(events) == 1
    assert events[0]["batch_size"] == 3


# ----------------------------------------------------------------------
# Subclass created AFTER instrument (the __init_subclass__ hook)
# ----------------------------------------------------------------------


def test_subclass_created_after_instrument_is_captured(tmp_path) -> None:
    storage = _boot(tmp_path)

    class LateVoyageEmbeddings(Embeddings):
        model = "voyage-3-lite"

        def embed_documents(self, texts):
            return [[0.0] for _ in texts]

        def embed_query(self, text):
            return [0.0]

    LateVoyageEmbeddings().embed_query("late binding")
    assert len(_events(storage)) == 1
    assert _events(storage)[0]["model"] == "voyage-3-lite"


# ----------------------------------------------------------------------
# Cross-layer dedup with the raw OpenAI shim
# ----------------------------------------------------------------------


def test_openai_embeddings_via_langchain_emits_once(tmp_path) -> None:
    """``OpenAIEmbeddings`` drives the OpenAI SDK underneath, so both
    the raw shim and this shim observe the call. The raw layer wins
    (exact provider usage) and the LangChain wrapper suppresses its
    duplicate."""
    fakes = install_fake_openai()
    fakes["embedding_options"]["prompt_tokens"] = 7
    import openai

    class OpenAIEmbeddings(Embeddings):
        model = "text-embedding-3-small"

        def embed_documents(self, texts):
            resp = openai.OpenAI().embeddings.create(model=self.model, input=texts)
            return [d["embedding"] for d in resp["data"]]

        def embed_query(self, text):
            resp = openai.OpenAI().embeddings.create(model=self.model, input=text)
            return resp["data"][0]["embedding"]

    storage = _boot(tmp_path, sdks=["openai"])
    OpenAIEmbeddings().embed_documents(["a", "b", "c"])

    events = _events(storage)
    assert len(events) == 1
    # The raw layer's event wins — exact usage, not an estimate.
    assert events[0]["provider"] == "openai"
    assert events[0]["input_tokens"] == 7
    assert events[0]["token_count_estimated"] is False


# ----------------------------------------------------------------------
# Lifecycle + inert handler hooks
# ----------------------------------------------------------------------


def test_shutdown_restores_methods(tmp_path) -> None:
    _boot(tmp_path)
    assert getattr(
        VoyageAIEmbeddings.embed_documents, "__inkfoot_embedding_shim__", False
    )
    instrument_mod.shutdown()
    assert not getattr(
        VoyageAIEmbeddings.embed_documents, "__inkfoot_embedding_shim__", False
    )


def test_off_by_default_does_not_patch(tmp_path) -> None:
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage, sdks=[], langchain=False)  # no embeddings=
    assert not getattr(
        VoyageAIEmbeddings.embed_documents, "__inkfoot_embedding_shim__", False
    )


def test_langchain_false_does_not_patch_embeddings(tmp_path) -> None:
    """``langchain=False`` means leave LangChain alone — even with
    ``embeddings=True`` the Embeddings classes stay untouched (the raw
    OpenAI shim still covers OpenAI)."""
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(
        storage=storage, sdks=[], langchain=False, embeddings=True
    )
    assert not getattr(
        VoyageAIEmbeddings.embed_documents, "__inkfoot_embedding_shim__", False
    )


def test_handler_embeddings_callbacks_are_inert(tmp_path) -> None:
    """The handler's on_embeddings_* are never called by LangChain;
    even if invoked directly they must not emit."""
    storage = _boot(tmp_path)
    handler = InkfootCallbackHandler()
    handler.on_embeddings_start({"id": ["x"]}, ["text"], run_id="abc")
    handler.on_embeddings_end(None, run_id="abc")
    assert _events(storage) == []
