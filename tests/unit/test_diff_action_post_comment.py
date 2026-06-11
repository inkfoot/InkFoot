"""Unit tests for the GitHub Action's sticky-comment poster.

The script lives in ``actions/diff-action/post_comment.py`` and is
not part of the importable ``inkfoot`` package; we load it from the
filesystem and exercise the :class:`GitHubClient` plus the ``main``
entry point against a fake HTTP layer.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any, Optional

import pytest


_POSTER_PATH = (
    Path(__file__).resolve().parents[2]
    / "actions"
    / "diff-action"
    / "post_comment.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "_inkfoot_diff_action_post_comment", str(_POSTER_PATH)
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def poster_module():
    return _load_module()


class _FakeClient:
    """Stand-in for :class:`GitHubClient` that records calls."""

    def __init__(self, existing_id: Optional[int] = None) -> None:
        self.existing_id = existing_id
        self.find_calls: list[tuple[str, int]] = []
        self.created: list[tuple[str, int, str]] = []
        self.patched: list[tuple[str, int, str]] = []

    def find_sticky_comment(self, repo: str, pr_number: int) -> Optional[int]:
        self.find_calls.append((repo, pr_number))
        return self.existing_id

    def create_comment(self, repo: str, pr_number: int, body: str) -> int:
        self.created.append((repo, pr_number, body))
        return 999

    def patch_comment(self, repo: str, comment_id: int, body: str) -> None:
        self.patched.append((repo, comment_id, body))


def test_main_creates_comment_when_no_existing_sticky(
    tmp_path, monkeypatch, poster_module
):
    body_path = tmp_path / "diff.md"
    body_path.write_text("<!-- inkfoot-diff-action -->\nHello")

    fake = _FakeClient(existing_id=None)
    monkeypatch.setattr(
        poster_module, "GitHubClient", lambda **_: fake
    )

    exit_code = poster_module.main(
        [
            "--pr-number",
            "42",
            "--repo",
            "owner/repo",
            "--body-file",
            str(body_path),
            "--token",
            "ghs_test",
        ]
    )
    assert exit_code == 0
    assert len(fake.created) == 1
    assert fake.created[0][:2] == ("owner/repo", 42)
    assert "<!-- inkfoot-diff-action -->" in fake.created[0][2]


def test_main_updates_existing_sticky_when_present(
    tmp_path, monkeypatch, poster_module
):
    body_path = tmp_path / "diff.md"
    body_path.write_text("<!-- inkfoot-diff-action -->\nUpdated")
    fake = _FakeClient(existing_id=777)
    monkeypatch.setattr(poster_module, "GitHubClient", lambda **_: fake)

    exit_code = poster_module.main(
        [
            "--pr-number",
            "9",
            "--repo",
            "x/y",
            "--body-file",
            str(body_path),
            "--token",
            "ghs_test",
        ]
    )
    assert exit_code == 0
    assert fake.created == []
    assert len(fake.patched) == 1
    assert fake.patched[0][1] == 777
    assert "Updated" in fake.patched[0][2]


def test_main_injects_marker_when_body_missing_it(
    tmp_path, monkeypatch, poster_module
):
    body_path = tmp_path / "diff.md"
    body_path.write_text("No marker here.")
    fake = _FakeClient(existing_id=None)
    monkeypatch.setattr(poster_module, "GitHubClient", lambda **_: fake)

    exit_code = poster_module.main(
        [
            "--pr-number",
            "1",
            "--repo",
            "x/y",
            "--body-file",
            str(body_path),
            "--token",
            "ghs_test",
        ]
    )
    assert exit_code == 0
    assert fake.created[0][2].startswith("<!-- inkfoot-diff-action -->")


def test_main_requires_token(tmp_path, monkeypatch, poster_module, capsys):
    body_path = tmp_path / "diff.md"
    body_path.write_text("body")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    exit_code = poster_module.main(
        [
            "--pr-number",
            "1",
            "--repo",
            "x/y",
            "--body-file",
            str(body_path),
        ]
    )
    assert exit_code == 2
    assert "GITHUB_TOKEN" in capsys.readouterr().err


def test_github_client_find_sticky_returns_matching_id(
    poster_module, monkeypatch
):
    client = poster_module.GitHubClient(token="t", api_base="https://api.github.com")
    pages = [
        [
            {"id": 1, "body": "irrelevant"},
            {"id": 2, "body": "<!-- inkfoot-diff-action -->\nfound me"},
        ],
    ]

    def fake_request(method, path, *, payload=None, query=None):
        return pages.pop(0)

    monkeypatch.setattr(client, "_request", fake_request)
    assert client.find_sticky_comment("o/r", 1) == 2


def test_github_client_find_sticky_returns_none_when_absent(
    poster_module, monkeypatch
):
    client = poster_module.GitHubClient(token="t", api_base="https://api.github.com")

    def fake_request(method, path, *, payload=None, query=None):
        return [{"id": 1, "body": "no marker"}]

    monkeypatch.setattr(client, "_request", fake_request)
    assert client.find_sticky_comment("o/r", 1) is None


def test_github_client_find_sticky_stops_at_pagination_cap(
    poster_module, monkeypatch, caplog
):
    # A PR with absurdly many unrelated comments must
    # not burn unbounded action minutes scanning every page.
    client = poster_module.GitHubClient(token="t", api_base="https://api.github.com")
    pages_walked: list[int] = []

    def fake_request(method, path, *, payload=None, query=None):
        page = query["page"] if query else 1
        pages_walked.append(page)
        # Return a full page of 100 comments forever — no marker.
        return [{"id": i + page * 100, "body": "no marker"} for i in range(100)]

    monkeypatch.setattr(client, "_request", fake_request)
    with caplog.at_level("WARNING"):
        result = client.find_sticky_comment("o/r", 1)
    assert result is None
    assert len(pages_walked) == poster_module.GitHubClient._MAX_COMMENT_PAGES
    assert any("pagination cap" in record.message for record in caplog.records)
