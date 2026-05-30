# CI cost review on GitLab

The current release ships a first-class GitHub Action; on every other CI system
the CLI does the same job. This recipe shows how to wire
`inkfoot benchmark` + `inkfoot diff` into a GitLab pipeline so merge
requests get a deterministic cost-review verdict.

## Pipeline

```yaml
# .gitlab-ci.yml
stages:
  - cost-review

cost-review:
  stage: cost-review
  image: python:3.12-slim
  variables:
    BASELINE_ARTIFACT: baseline.json
    CURRENT_ARTIFACT: current.json
  rules:
    # Only run on MRs that touch agent code (cost spend hygiene).
    - if: $CI_PIPELINE_SOURCE == 'merge_request_event'
      changes:
        - "src/agents/**/*"
        - "tests/agent_scenarios/**/*"
  before_script:
    - pip install --quiet "inkfoot[all]"
  script:
    - |
      # 1. Fetch the latest main-branch baseline artefact.
      curl --silent --location \
        --header "PRIVATE-TOKEN: ${CI_JOB_TOKEN}" \
        "${CI_API_V4_URL}/projects/${CI_PROJECT_ID}/jobs/artifacts/main/raw/${BASELINE_ARTIFACT}?job=cost-review" \
        --output "${BASELINE_ARTIFACT}" \
        || cp tests/agent_scenarios/seed_baseline.json "${BASELINE_ARTIFACT}"

      # 2. Run the benchmark against the MR head.
      inkfoot benchmark ./tests/agent_scenarios \
        --output "${CURRENT_ARTIFACT}" \
        --quiet

      # 3. Diff against the baseline (Markdown for the MR body, JSON for downstream jobs).
      inkfoot diff "${BASELINE_ARTIFACT}" "${CURRENT_ARTIFACT}" \
        --format markdown \
        --output cost-diff.md
      inkfoot diff "${BASELINE_ARTIFACT}" "${CURRENT_ARTIFACT}" \
        --format json \
        --output cost-diff.json
  artifacts:
    paths:
      - "${CURRENT_ARTIFACT}"
      - cost-diff.md
      - cost-diff.json
    when: always
```

## Posting back to the MR

GitLab MRs accept Markdown notes via the REST API. After the
`cost-review` job a follow-up `post-mr-note` job (or a
`merge_request_pipelines` rule) can use `curl`:

```yaml
post-mr-note:
  stage: cost-review
  needs: ["cost-review"]
  image: alpine:3.20
  rules:
    - if: $CI_PIPELINE_SOURCE == 'merge_request_event'
  before_script:
    - apk add --no-cache curl jq
  script:
    - |
      BODY=$(jq -Rs '.' < cost-diff.md)
      curl --silent --request POST \
        --header "PRIVATE-TOKEN: ${GITLAB_BOT_TOKEN}" \
        --header "Content-Type: application/json" \
        --data "{\"body\": ${BODY}}" \
        "${CI_API_V4_URL}/projects/${CI_PROJECT_ID}/merge_requests/${CI_MERGE_REQUEST_IID}/notes" \
        > /dev/null
```

`GITLAB_BOT_TOKEN` must have at least `api` scope on the project. To
keep the comment "sticky" the same way the GitHub Action does, query
existing notes for the `<!-- inkfoot-diff-action -->` marker (the
Markdown renderer embeds it automatically) and `PUT` instead of
`POST` when one already exists.

## Cost guardrails

the benchmark makes real LLM calls. Practical guardrails:

- Path-filter so non-agent MRs skip the job (the `rules.changes`
  block above does this).
- Keep scenario fixture sets small (3–5 fixtures × 1 run).
- Surface the spend in MR descriptions (the diff Markdown already
  reports cost deltas).
