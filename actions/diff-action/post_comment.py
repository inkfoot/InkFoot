"""Sticky-comment poster for the inkfoot/diff-action GitHub Action.

identify our own comment via a hidden HTML marker, and
update it on subsequent pushes instead of appending a new one. The
marker (``<!-- inkfoot-diff-action -->``) is embedded by
:mod:`inkfoot.diff.render_markdown` so the diff CLI and the action
agree.

This script talks to the GitHub REST API directly using urllib so it
adds zero new runtime dependencies beyond Python's stdlib. The
authentication token is read from ``GITHUB_TOKEN`` (set by the
action's ``inputs.github-token``).

The CLI surface is small on purpose so the action's ``run:`` step
can call it with just three flags:

    python post_comment.py \
        --pr-number 42 \
        --repo owner/repo \
        --body-file /path/to/diff.md
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Optional


STICKY_COMMENT_MARKER = "<!-- inkfoot-diff-action -->"

_LOG = logging.getLogger("inkfoot.diff_action.post_comment")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="post_comment.py")
    parser.add_argument("--pr-number", type=int, required=True)
    parser.add_argument(
        "--repo",
        required=True,
        help="`owner/name` form, as GitHub Actions provides.",
    )
    parser.add_argument(
        "--body-file",
        required=True,
        help="Path to a Markdown file containing the comment body.",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("GITHUB_TOKEN"),
        help="GitHub token. Defaults to $GITHUB_TOKEN.",
    )
    parser.add_argument(
        "--api-base",
        default=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        help="GitHub REST API base. Honours GHES via $GITHUB_API_URL.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not args.token:
        print(
            "post_comment: GITHUB_TOKEN is required (pass --token or set the env var).",
            file=sys.stderr,
        )
        return 2

    body = _read_body(args.body_file)
    if STICKY_COMMENT_MARKER not in body:
        # Defence against accidentally posting an unrelated body — the
        # marker is how we find this comment on the next push. If it
        # isn't present, future runs would post a duplicate instead
        # of updating.
        _LOG.warning(
            "post_comment: marker %r not in body; injecting it so future "
            "runs can find this comment",
            STICKY_COMMENT_MARKER,
        )
        body = f"{STICKY_COMMENT_MARKER}\n{body}"

    client = GitHubClient(token=args.token, api_base=args.api_base)
    existing_id = client.find_sticky_comment(args.repo, args.pr_number)
    if existing_id is not None:
        client.patch_comment(args.repo, existing_id, body)
        _LOG.info("post_comment: updated sticky comment id=%s", existing_id)
    else:
        new_id = client.create_comment(args.repo, args.pr_number, body)
        _LOG.info("post_comment: posted new sticky comment id=%s", new_id)
    return 0


def _read_body(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


class GitHubClient:
    """Tiny wrapper over the issue-comments REST API.

    Surface mirrors the three operations the sticky-comment logic
    needs: list comments to find our own, PATCH our own when present,
    POST a new one otherwise. Extracted as a class so the unit tests
    can mock the underlying ``_request`` method without monkey-
    patching ``urllib``."""

    def __init__(self, *, token: str, api_base: str) -> None:
        self._token = token
        self._api_base = api_base.rstrip("/")

    # ------------------------------------------------------------------
    # Public surface.
    # ------------------------------------------------------------------

    # Cap the comment-pagination walk so a PR with an absurd number
    # of unrelated comments can't burn unbounded action minutes
    # (Finding #12). 20 × 100 = 2000 comments is far past any realistic
    # PR; if we hit the cap without finding a marker we log and treat
    # it as "no sticky comment present", which means the next push
    # would append a fresh one (still better than billing forever).
    _MAX_COMMENT_PAGES = 20

    def find_sticky_comment(self, repo: str, pr_number: int) -> Optional[int]:
        """Return the id of an existing inkfoot-diff comment, or
        ``None`` if no prior comment exists for this PR."""
        for page in range(1, self._MAX_COMMENT_PAGES + 1):
            comments = self._request(
                "GET",
                f"/repos/{repo}/issues/{pr_number}/comments",
                query={"per_page": 100, "page": page},
            )
            if not comments:
                return None
            for c in comments:
                body = c.get("body") or ""
                if STICKY_COMMENT_MARKER in body:
                    return int(c["id"])
            if len(comments) < 100:
                return None
        _LOG.warning(
            "find_sticky_comment: reached pagination cap (%d pages) "
            "without finding the marker on %s#%s; treating as absent",
            self._MAX_COMMENT_PAGES,
            repo,
            pr_number,
        )
        return None

    def create_comment(self, repo: str, pr_number: int, body: str) -> int:
        result = self._request(
            "POST",
            f"/repos/{repo}/issues/{pr_number}/comments",
            payload={"body": body},
        )
        return int(result["id"])

    def patch_comment(self, repo: str, comment_id: int, body: str) -> None:
        self._request(
            "PATCH",
            f"/repos/{repo}/issues/comments/{comment_id}",
            payload={"body": body},
        )

    # ------------------------------------------------------------------
    # Plumbing.
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[dict[str, Any]] = None,
        query: Optional[dict[str, Any]] = None,
    ) -> Any:
        url = f"{self._api_base}{path}"
        if query:
            from urllib.parse import urlencode

            url = f"{url}?{urlencode(query)}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, method=method, data=data)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("User-Agent", "inkfoot-diff-action")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"GitHub API {method} {url} failed: HTTP {exc.code}\n{detail}"
            ) from exc
        if not body:
            return None
        return json.loads(body)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
