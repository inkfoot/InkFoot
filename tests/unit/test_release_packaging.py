"""Packaging + release-pipeline gate for the early-access PyPI release.

The early-access release ships three things: the framework-adapter
extras in ``pyproject.toml``, the tag-triggered publish workflow, and
the post-publish smoke workflow. These tests assert the static shape of
all three so a regression — a dropped extra, a leaked ``langchain``
extra (deferred to a later release), a publish workflow that quietly
stops using Trusted Publishing — fails CI instead of surfacing at
release time.

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
PRERELEASE_WF = WORKFLOWS / "release-prerelease.yml"
SMOKE_WF = WORKFLOWS / "release-smoke.yml"


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


# --- T2: extras ----------------------------------------------------------

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
def test_all_meta_extra_bundles_every_framework_extra():
    extras = _load_toml()["project"]["optional-dependencies"]
    assert "all" in extras
    joined = " ".join(extras["all"])
    for name in (
        "langgraph",
        "openai-agents",
        "anthropic-agent",
        "pydantic-ai",
        "crewai",
    ):
        assert name in joined, f"[all] should pull in {name}"


@toml_required
def test_langchain_extra_not_declared():
    # `[langchain]` is deferred to a later release; it must not leak
    # into the early-access release.
    extras = _load_toml()["project"]["optional-dependencies"]
    assert "langchain" not in extras


@toml_required
def test_supports_declared_python_versions():
    # Acceptance: works on Python 3.10/3.11/3.12.
    project = _load_toml()["project"]
    assert project["requires-python"] == ">=3.10"
    classifiers = " ".join(project["classifiers"])
    for minor in ("3.10", "3.11", "3.12"):
        assert minor in classifiers


# --- version is a pre-release -------------------------------------------

def test_shipped_version_is_a_prerelease():
    # The early-access pipeline only publishes a/b/rc releases; the
    # package's own version must be one too.
    spec = importlib.util.spec_from_file_location(
        "check_prerelease_tag", REPO_ROOT / "scripts" / "check_prerelease_tag.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    from inkfoot._version import __version__

    assert module.is_prerelease(__version__), (
        f"_version.py declares '{__version__}', which is not a pre-release; "
        f"the early-access pipeline can only publish a/b/rc versions."
    )


# --- T1: publish workflow -----------------------------------------------

@yaml_required
def test_prerelease_workflow_exists_and_parses():
    assert PRERELEASE_WF.exists(), f"missing {PRERELEASE_WF}"
    assert _load_yaml(PRERELEASE_WF)  # parses to a non-empty mapping


@yaml_required
def test_prerelease_workflow_is_tag_triggered_on_prereleases_only():
    wf = _load_yaml(PRERELEASE_WF)
    # PyYAML parses the bare `on:` key as the boolean True.
    on = wf.get("on", wf.get(True))
    tags = on["push"]["tags"]
    joined = " ".join(tags)
    # Pre-release markers present...
    assert "a[0-9]" in joined and "b[0-9]" in joined and "rc[0-9]" in joined
    # ...and no final-release glob (e.g. v[0-9]+.[0-9]+.[0-9]+ on its own).
    for pat in tags:
        assert any(m in pat for m in ("a[0-9]", "b[0-9]", "rc[0-9]")), (
            f"tag pattern {pat!r} would match a final release"
        )


@yaml_required
def test_prerelease_workflow_uses_trusted_publishing():
    wf = _load_yaml(PRERELEASE_WF)
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
def test_prerelease_workflow_runs_the_tag_guard():
    wf = _load_yaml(PRERELEASE_WF)
    runs = " ".join(
        step.get("run", "") for step in wf["jobs"]["guard"]["steps"]
    )
    assert "check_prerelease_tag.py" in runs


@yaml_required
def test_prerelease_publish_gated_to_canonical_repo():
    wf = _load_yaml(PRERELEASE_WF)
    assert "inkfoot/inkfoot" in str(wf["jobs"]["guard"].get("if", ""))


@yaml_required
def test_guard_job_never_imports_inkfoot():
    # Regression for the workflow_dispatch crash: importing the package
    # in the guard job (which installs no deps) fails on missing
    # tiktoken. The guard must text-parse the version via the script's
    # --from-package mode instead.
    wf = _load_yaml(PRERELEASE_WF)
    runs = " ".join(step.get("run", "") for step in wf["jobs"]["guard"]["steps"])
    assert "import inkfoot" not in runs
    assert "--from-package" in runs


@yaml_required
def test_publish_workflow_creates_github_release_to_fire_smoke():
    # The smoke workflow triggers on `release: published`; nothing else
    # in the pipeline creates a Release, so the publish workflow must.
    # Without this step the smoke gate (T3) only ever runs via manual
    # dispatch.
    wf = _load_yaml(PRERELEASE_WF)
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
    # Must be a pre-release (not the public launch) and needs
    # contents: write to create the Release.
    assert job["permissions"]["contents"] == "write"
    gh_step = next(
        s for s in job["steps"] if "action-gh-release" in s.get("uses", "")
    )
    assert gh_step["with"]["prerelease"] is True


# --- T3: smoke workflow --------------------------------------------------

@yaml_required
def test_smoke_workflow_exists_and_parses():
    assert SMOKE_WF.exists(), f"missing {SMOKE_WF}"
    assert _load_yaml(SMOKE_WF)


@yaml_required
def test_smoke_workflow_matrixes_supported_pythons():
    wf = _load_yaml(SMOKE_WF)
    versions = wf["jobs"]["smoke"]["strategy"]["matrix"]["python-version"]
    assert set(versions) == {"3.10", "3.11", "3.12"}


@yaml_required
def test_smoke_workflow_installs_prerelease_from_pypi():
    wf = _load_yaml(SMOKE_WF)
    runs = " ".join(
        step.get("run", "") for step in wf["jobs"]["smoke"]["steps"]
    )
    # `--pre` is what lets pip resolve an a/b/rc release.
    assert "--pre" in runs
    assert "pip install" in runs


@yaml_required
def test_smoke_workflow_runs_in_clean_container():
    wf = _load_yaml(SMOKE_WF)
    image = wf["jobs"]["smoke"]["container"]["image"]
    assert "python:" in image


@yaml_required
def test_smoke_workflow_fires_on_published_release():
    # The far end of the trigger chain: the publish workflow creates a
    # pre-release, and this is the event that catches it. (PyYAML maps a
    # bare `on:` key to the boolean True.)
    wf = _load_yaml(SMOKE_WF)
    on = wf.get("on", wf.get(True))
    assert "release" in on
    assert "published" in on["release"]["types"]
