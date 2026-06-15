"""Packaging + release-pipeline gate for the public release.

The public release ships three things: the framework-adapter and
tooling extras in ``pyproject.toml``, the tag-triggered publish
workflow, and the post-publish smoke workflow. These tests assert the
static shape of all three so a regression — a dropped extra, a publish
workflow that quietly stops using Trusted Publishing, a smoke matrix
that loses an interpreter — fails CI instead of surfacing at release
time.

YAML/TOML parsing skips when the parser isn't importable; both ship in
the dev extra (``pip install -e ".[dev]"``) which CI installs.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
RELEASE_WF = WORKFLOWS / "release.yml"
SMOKE_WF = WORKFLOWS / "release-smoke.yml"

# The full set of interpreters the package promises to support.
SUPPORTED_PYTHONS = {"3.10", "3.11", "3.12", "3.13"}


def _have(mod: str) -> bool:
    return importlib.util.find_spec(mod) is not None


def _load_toml() -> dict:
    if _have("tomllib"):  # py3.11+
        import tomllib

        return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    import tomli  # type: ignore

    return tomli.loads(PYPROJECT.read_text(encoding="utf-8"))


def _load_yaml(path: Path) -> dict:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


toml_required = pytest.mark.skipif(
    not (_have("tomllib") or _have("tomli")),
    reason="no TOML parser available",
)
yaml_required = pytest.mark.skipif(
    not _have("yaml"),
    reason="pyyaml not installed (install with: pip install -e \".[dev]\")",
)


# --- extras --------------------------------------------------------------


@toml_required
def test_framework_extras_declared():
    extras = _load_toml()["project"]["optional-dependencies"]
    for name in (
        "langgraph",
        "openai-agents",
        "anthropic-agent",
        "pydantic-ai",
        "crewai",
    ):
        assert name in extras, f"missing framework extra: {name}"
        assert extras[name], f"extra {name} declares no requirements"


@toml_required
def test_each_extra_pins_its_peer_framework():
    extras = _load_toml()["project"]["optional-dependencies"]
    expected_peer = {
        "langchain": "langchain-core",
        "langgraph": "langgraph",
        "openai-agents": "openai-agents",
        "anthropic-agent": "anthropic-agent",
        "pydantic-ai": "pydantic-ai",
        "crewai": "crewai",
    }
    for extra, peer in expected_peer.items():
        joined = " ".join(extras[extra])
        assert peer in joined, f"extra {extra} should pin {peer}, got {extras[extra]}"


@toml_required
def test_provider_extras_declared():
    extras = _load_toml()["project"]["optional-dependencies"]
    expected_sdk = {
        "gemini": "google-generativeai",
        "bedrock": "boto3",
    }
    for extra, sdk in expected_sdk.items():
        assert extra in extras, f"missing provider extra: {extra}"
        joined = " ".join(extras[extra])
        assert sdk in joined, f"extra {extra} should pin {sdk}, got {extras[extra]}"


@toml_required
def test_storage_extra_declared():
    extras = _load_toml()["project"]["optional-dependencies"]
    assert "postgres" in extras
    assert "psycopg" in " ".join(extras["postgres"])


@toml_required
def test_lint_extra_declared_with_ruff():
    # The lint toolchain ships as its own extra so contributors can pull
    # it without the full dev install.
    extras = _load_toml()["project"]["optional-dependencies"]
    assert "lint" in extras, "missing [lint] extra"
    joined = " ".join(extras["lint"])
    assert "ruff" in joined, f"[lint] should pin ruff, got {extras['lint']}"


@toml_required
def test_all_meta_extra_bundles_every_framework_and_provider_extra():
    extras = _load_toml()["project"]["optional-dependencies"]
    assert "all" in extras
    joined = " ".join(extras["all"])
    for name in (
        "langchain",
        "langgraph",
        "openai-agents",
        "anthropic-agent",
        "pydantic-ai",
        "crewai",
        "gemini",
        "bedrock",
    ):
        assert name in joined, f"[all] should pull in {name}"


@toml_required
def test_langchain_extra_pins_only_langchain_core():
    # The callback handler needs langchain-core (BaseCallbackHandler +
    # the normalised usage shapes) and nothing else — the per-provider
    # partner packages are the user's to choose.
    extras = _load_toml()["project"]["optional-dependencies"]
    assert "langchain" in extras
    joined = " ".join(extras["langchain"])
    assert "langchain-core" in joined
    for partner in (
        "langchain-anthropic",
        "langchain-openai",
        "langchain-google-genai",
        "langchain-aws",
    ):
        assert partner not in joined, (
            f"[langchain] must not pull the {partner} partner package"
        )


@toml_required
def test_supports_declared_python_versions():
    project = _load_toml()["project"]
    assert project["requires-python"] == ">=3.10"
    classifiers = " ".join(project["classifiers"])
    for minor in sorted(SUPPORTED_PYTHONS):
        assert minor in classifiers, f"missing classifier for Python {minor}"


@toml_required
def test_classifier_marks_production_stable():
    # A 1.0 GA release should not still advertise itself as Alpha.
    classifiers = _load_toml()["project"]["classifiers"]
    statuses = [c for c in classifiers if c.startswith("Development Status")]
    assert statuses == ["Development Status :: 5 - Production/Stable"], statuses


# --- version is a final release -----------------------------------------


def test_shipped_version_is_a_final_release():
    # The public pipeline only publishes final releases; the package's
    # own version must be one too.
    spec = importlib.util.spec_from_file_location(
        "check_release_tag", REPO_ROOT / "scripts" / "check_release_tag.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    from inkfoot._version import __version__

    assert module.is_final_release(__version__), (
        f"_version.py declares '{__version__}', which is not a final release; "
        f"the public pipeline can only publish final versions."
    )


# --- publish workflow ----------------------------------------------------


@yaml_required
def test_release_workflow_exists_and_parses():
    assert RELEASE_WF.exists(), f"missing {RELEASE_WF}"
    assert _load_yaml(RELEASE_WF)  # parses to a non-empty mapping


@yaml_required
def test_release_workflow_is_tag_triggered_on_final_releases_only():
    wf = _load_yaml(RELEASE_WF)
    # PyYAML parses the bare `on:` key as the boolean True.
    on = wf.get("on", wf.get(True))
    tags = on["push"]["tags"]
    joined = " ".join(tags)
    # A final-release glob is present...
    assert "[0-9]+.[0-9]+.[0-9]+" in joined
    # ...and no pre-release marker that would fire on an a/b/rc tag.
    for pat in tags:
        assert not any(
            m in pat for m in ("a[0-9]", "b[0-9]", "rc[0-9]")
        ), f"tag pattern {pat!r} would match a pre-release"


@yaml_required
def test_release_workflow_uses_trusted_publishing():
    wf = _load_yaml(RELEASE_WF)
    publish = wf["jobs"]["publish"]
    # Trusted Publishing requires id-token: write on the publishing job.
    assert publish["permissions"]["id-token"] == "write"
    steps = publish["steps"]
    uses = [s.get("uses", "") for s in steps]
    assert any("pypa/gh-action-pypi-publish" in u for u in uses)
    # No password/token wired in — OIDC supplies auth.
    for step in steps:
        with_block = step.get("with") or {}
        assert "password" not in with_block


@yaml_required
def test_release_workflow_runs_the_tag_guard():
    wf = _load_yaml(RELEASE_WF)
    runs = " ".join(step.get("run", "") for step in wf["jobs"]["guard"]["steps"])
    assert "check_release_tag.py" in runs


@yaml_required
def test_release_publish_gated_to_canonical_repo():
    wf = _load_yaml(RELEASE_WF)
    assert "inkfoot/inkfoot" in str(wf["jobs"]["guard"].get("if", ""))


@yaml_required
def test_guard_job_never_imports_inkfoot():
    # Importing the package in the guard job (which installs no deps)
    # would fail on missing tiktoken. The guard must text-parse the
    # version via the script's --from-package mode instead.
    wf = _load_yaml(RELEASE_WF)
    runs = " ".join(step.get("run", "") for step in wf["jobs"]["guard"]["steps"])
    assert "import inkfoot" not in runs
    assert "--from-package" in runs


@yaml_required
def test_publish_workflow_creates_public_github_release_to_fire_smoke():
    # The smoke workflow triggers on `release: published`; nothing else
    # in the pipeline creates a Release, so the publish workflow must —
    # and as a public (non-pre) release.
    wf = _load_yaml(RELEASE_WF)
    jobs = wf["jobs"]
    release_jobs = [
        j
        for j in jobs.values()
        if any(
            "action-gh-release" in step.get("uses", "")
            for step in j.get("steps", [])
        )
    ]
    assert release_jobs, "no job creates a GitHub Release to fire the smoke trigger"
    job = release_jobs[0]
    assert job["permissions"]["contents"] == "write"
    gh_step = next(
        s for s in job["steps"] if "action-gh-release" in s.get("uses", "")
    )
    assert gh_step["with"]["prerelease"] is False


# --- smoke workflow ------------------------------------------------------


@yaml_required
def test_smoke_workflow_exists_and_parses():
    assert SMOKE_WF.exists(), f"missing {SMOKE_WF}"
    assert _load_yaml(SMOKE_WF)


@yaml_required
def test_smoke_workflow_matrixes_every_supported_python():
    wf = _load_yaml(SMOKE_WF)
    versions = wf["jobs"]["smoke"]["strategy"]["matrix"]["python-version"]
    assert set(versions) == SUPPORTED_PYTHONS


@yaml_required
def test_smoke_workflow_installs_final_release_from_pypi():
    wf = _load_yaml(SMOKE_WF)
    # Consider only executable lines, not comments: a comment that
    # explains why `--pre` is absent must not trip the check.
    install_lines = []
    for step in wf["jobs"]["smoke"]["steps"]:
        for line in step.get("run", "").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "pip install" in stripped:
                install_lines.append(stripped)
    assert install_lines, "smoke must pip install inkfoot"
    assert any("inkfoot" in ln for ln in install_lines)
    # The public pipeline ships finals only — no install line may
    # resolve a pre-release via `--pre`.
    for ln in install_lines:
        assert "--pre" not in ln, f"smoke install must not use --pre: {ln}"


@yaml_required
def test_smoke_workflow_runs_in_clean_container():
    wf = _load_yaml(SMOKE_WF)
    image = wf["jobs"]["smoke"]["container"]["image"]
    assert "python:" in image


@yaml_required
def test_smoke_workflow_fires_on_published_release():
    # The far end of the trigger chain: the publish workflow creates a
    # release, and this is the event that catches it. (PyYAML maps a
    # bare `on:` key to the boolean True.)
    wf = _load_yaml(SMOKE_WF)
    on = wf.get("on", wf.get(True))
    assert "release" in on
    assert "published" in on["release"]["types"]
