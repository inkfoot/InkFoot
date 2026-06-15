# Contributing to Inkfoot

Thanks for your interest in improving Inkfoot. This guide covers how to
set up a development environment, the conventions the codebase follows,
and what a good pull request looks like.

By participating you agree to abide by our
[Code of Conduct](CODE_OF_CONDUCT.md).

## Ways to contribute

- **Report a bug** or **request a feature** using the issue templates.
- **Propose a cost smell** for the community smell library.
- **Flag a provider-coverage or LangChain-integration gap** — these
  have dedicated templates because they are the most common source of
  "Inkfoot didn't capture my call."
- **Improve the docs** — fixes to the guides on
  [inkfoot.dev](https://inkfoot.dev) are very welcome.
- **Send code** — see below.

## Development setup

Inkfoot targets Python 3.10–3.13 and has no required runtime
dependencies beyond a small, pure-Python core.

```bash
# 1. Clone your fork
git clone https://github.com/<you>/inkfoot.git
cd inkfoot

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Install the package with the dev extra (tests + tooling)
pip install -e ".[dev]"
```

Optional extras you may want depending on what you are working on:

| Extra | When you need it |
|---|---|
| `[lint]` | Running the linters / type checker (`ruff`, `mypy`). |
| `[docs]` | Building or serving the docs site locally. |
| `[postgres]` | Touching the Postgres storage backend. |
| `[all]` | Exercising every framework adapter and provider extra. |

Install several at once, e.g. `pip install -e ".[dev,lint,docs]"`.

## Running the tests

The default unit, property, and docs-link suites are fast and need no
credentials:

```bash
pytest tests/unit tests/property tests/docs
```

Other suites:

```bash
# Whole test tree (integration + contract included)
pytest

# Coverage, mirroring CI's floor
pytest tests/unit tests/contract tests/integration tests/property \
  --cov=inkfoot --cov-report=term-missing --cov-fail-under=80
```

**Live tests** that hit a real provider endpoint are marked (for
example `live_anthropic`, `live_openai`) and **skip by default** — they
run on a weekly schedule in CI, not on every pull request. You do not
need provider credentials to contribute.

## Linting and types

```bash
ruff check .
ruff format --check .
mypy inkfoot
```

Please run these before opening a pull request; CI runs them too.

## Building the docs

```bash
pip install -e ".[docs]"
mkdocs serve   # live-reload preview at http://127.0.0.1:8000
```

The docs build runs with `--strict`, so a broken internal link or an
orphaned page fails the build. Add new pages to the `nav` in
`mkdocs.yml`.

## Conventions

- **Public vs. private surface.** Anything under an underscore-prefixed
  module (e.g. `inkfoot._instrument`) is internal and not part of the
  SemVer contract. The public surface is what `inkfoot` re-exports plus
  the documented submodules.
- **Tests are part of the change.** New behaviour ships with tests that
  cover the happy path *and* the edges (empty input, boundaries, error
  paths). A bug fix ships with a regression test that fails without the
  fix.
- **Match the surrounding style.** Type hints throughout, small named
  functions over large procedures, explicit error handling, and
  comments that explain *why* rather than *what*.
- **Keep instrumentation safe.** Capture code runs inside other
  people's programs: a hook that raises must never break the host call.
  Wrap new hooks accordingly and add an isolation test.

## Pull request checklist

Before you open a PR, please confirm:

- [ ] Tests pass locally (`pytest tests/unit tests/property tests/docs`).
- [ ] Linters and type checks are clean (`ruff`, `mypy`).
- [ ] New or changed behaviour has tests, including edge cases.
- [ ] Docs are updated for any user-visible change (and the strict docs
      build passes).
- [ ] `CHANGELOG.md` has an entry under the unreleased section if the
      change is user-visible.
- [ ] The PR description explains the motivation and the approach.

Open the PR against `main`. CI must be green and a maintainer review is
required before merge.

## Reporting security issues

Please do **not** open a public issue for security vulnerabilities.
Follow the process in [SECURITY.md](SECURITY.md) instead.
