# inkfoot/diff-action

GitHub composite Action that runs `inkfoot benchmark` + `inkfoot diff`
against a baseline benchmark artefact and posts a sticky PR comment
with the result. Phase 1 / E2-S3 deliverable.

> The Action source lives in this directory of the `inkfoot/inkfoot`
> repository during Phase 1 development; it will be lifted into its
> own `inkfoot/diff-action` repository ahead of the Marketplace
> publish step (E2-S3 / T5). See [Marketplace migration plan](#marketplace-migration-plan)
> below for the checklist.

## Usage

```yaml
# .github/workflows/cost-review.yml
on:
  pull_request:
    paths: ["src/agents/**", "tests/agent_scenarios/**"]

jobs:
  cost:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write
    steps:
      - uses: actions/checkout@v4
      - uses: inkfoot/diff-action@v1
        with:
          scenarios: ./tests/agent_scenarios
          # Pull the most recent successful main-branch baseline
          # artefact uploaded by the prior workflow run.
          baseline-source: artifact:cost-review.yml:inkfoot-benchmark-current
          fail-threshold: default
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
```

## Inputs

| Input | Default | Notes |
|---|---|---|
| `scenarios` | `tests/agent_scenarios` | Directory of scenario `.py` files. |
| `baseline-source` | `path:baseline.json` | DSL: `path:<file>` / `artifact:<workflow>:<artifact-name>` / `release:<tag>:<asset>`. See "Baseline source" below. |
| `current-output` | `current.json` | Where the new benchmark JSON gets written; uploaded as an artefact. |
| `fail-threshold` | `default` | `tight` / `default` / `loose` preset, or a path to a JSON file. |
| `python-version` | `3.12` | Forwarded to `setup-python`. |
| `inkfoot-version` | _latest_ | Pin a specific PyPI version. |
| `inkfoot-extras` | `all` | Extras passed to pip (`inkfoot[<extras>]`). Empty string = bare install. Defaults to `all` so scenarios can import framework adapters out of the box (phase-1-explain §4.5 step 1). |
| `install-inkfoot` | `true` | Whether the action runs its own `pip install`. Set to `false` when a prior workflow step has already installed inkfoot (e.g. `pip install -e .` for an in-repo self-test). When set, the action still verifies `inkfoot` is importable and fails loudly if not. |
| `github-token` | `${{ github.token }}` | Needs `pull-requests: write` for the sticky comment. |
| `scenarios-only` | _all_ | Comma-separated task names to filter to. |

## Outputs

| Output | Description |
|---|---|
| `verdict` | `ok` / `warn` / `fail` — the diff verdict. |
| `diff-markdown-path` | Path to the rendered Markdown diff on the runner. |
| `baseline-path` | Local path the action resolved the baseline to. |

## Baseline source

The action accepts three baseline sources via the `baseline-source`
input:

* `path:<file>` — `<file>` is a path on the runner relative to the
  workflow's working directory. Useful when a prior step has already
  fetched the baseline (or for tests that commit a seed file).
* `artifact:<workflow-file>:<artifact-name>` — downloads
  `<artifact-name>` from the most recent successful main-branch run
  of `<workflow-file>` (e.g. `cost-review.yml`). Uses the
  pre-installed `gh` CLI; no extra setup required. The artefact must
  contain a JSON file matching `current-output`'s basename, or a
  single `.json` file the action can fall back to.
* `release:<tag>:<asset>` — downloads `<asset>` from the release
  tagged `<tag>`. Pass `release:latest:<asset>` to follow the latest
  release. Again, the pre-installed `gh` CLI does the work.

## Behaviour

1. Sets up Python (`setup-python`) at `python-version`.
2. Installs `inkfoot[all]` (or `inkfoot[<inkfoot-extras>]`, optionally
   pinned via `inkfoot-version`). The default extras make framework
   adapters available to scenario code without follow-up installs.
3. Resolves the baseline through the `baseline-source` DSL above.
4. Runs `inkfoot benchmark $scenarios --output $current-output --quiet`.
5. Runs `inkfoot diff $baseline-path $current-output --thresholds $fail-threshold --format markdown`.
6. Posts a sticky PR comment (one per PR, updated on subsequent
   pushes). Supports both `pull_request` and `pull_request_target`
   triggers.
7. Uploads `current.json` as a build artefact for the next baseline.
8. Exits with the inkfoot-diff exit code (`0` ok, `1` warn, `2` fail).

The sticky-comment marker is the HTML comment
`<!-- inkfoot-diff-action -->` (ADR-1-6). The Markdown renderer
embeds it automatically; `post_comment.py` re-injects it on the rare
path where it was stripped.

## Security

The action follows GitHub's hardening guidance for composite
actions: every `${{ inputs.* }}` value is surfaced into shell steps
through an `env:` block and dereferenced as `$VAR`, never inlined
into the script body. This blocks the standard shell-injection
attack vector against Marketplace actions whose inputs come from
third-party workflows.

## Self-test workflows

Two workflows live under `.github/workflows/` here and exercise the
action from different angles:

| Workflow | Validates | Trigger |
|---|---|---|
| `e2e.yml` | The in-repo `inkfoot` source (`pip install -e .` + `install-inkfoot: false`). Catches regressions in `inkfoot benchmark` / `inkfoot diff` code at PR time. | `pull_request` touching `actions/diff-action/**`, `workflow_dispatch`. |
| `release-smoke.yml` | The **published** `inkfoot` (PyPI). Catches release-time regressions that wouldn't surface from the in-repo build. | `release: { types: [published] }`, weekly cron, `workflow_dispatch`. |

The two are complementary: `e2e.yml` is the regression net for
in-tree changes; `release-smoke.yml` is the regression net for the
artefact Marketplace consumers actually run.

## Marketplace migration plan

E2-S3 / T5 splits this directory into its own `inkfoot/diff-action`
repository ahead of the Marketplace publish. The checklist:

1. `git subtree split --prefix=actions/diff-action -b diff-action-split`
   and push the resulting branch to a fresh `inkfoot/diff-action`
   repo as `main`.
2. Move `e2e.yml` and `release-smoke.yml` from
   `actions/diff-action/.github/workflows/` to the new repo's
   `.github/workflows/`. Update the `uses: ./actions/diff-action`
   reference in `e2e.yml` to `uses: ./` (the action is now at the
   repo root).
3. Add tag-driven release workflow (`.github/workflows/release.yml`)
   that runs the e2e and release-smoke jobs, then triggers the
   Marketplace publish action on the same tag. Tag with `v1.0.0`
   *and* a moving `v1` major-version tag (Marketplace convention).
4. In the original `inkfoot/inkfoot` repo, replace this directory
   with a `MOVED.md` pointer that links to
   `https://github.com/inkfoot/diff-action`.
5. Update consumer-facing docs (`InkFoot-docs/docs/cli-reference.md`,
   the Phase 1 epic doc, the launch blog post) to reference
   `inkfoot/diff-action@v1` rather than the in-repo path.

Until those steps complete, downstream workflows can already pin
against the in-repo source by checking out `inkfoot/inkfoot` and
using `uses: ./actions/diff-action` — useful for the maintainers'
own dogfooding ahead of the marketplace publish.
