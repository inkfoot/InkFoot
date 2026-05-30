# CI cost review on Bitbucket

Bitbucket Pipelines doesn't have native composite-action support, so
the recipe drives the `inkfoot` CLI directly. The flow is the same as
the GitHub Action:

1. Restore the previous baseline artefact.
2. Run `inkfoot benchmark` against the PR head.
3. Run `inkfoot diff` and post the Markdown back to the PR as a
   sticky comment.

## Pipeline

```yaml
# bitbucket-pipelines.yml
image: python:3.12

pipelines:
  pull-requests:
    "**":
      - step:
          name: Inkfoot cost review
          caches:
            - pip
          script:
            - pip install --quiet "inkfoot[all]"
            - BASELINE=baseline.json
            - CURRENT=current.json
            # 1. Download the baseline from the prior main pipeline's
            #    artifacts. The Bitbucket REST API exposes them as
            #    /artifacts/<filename>; fall back to a checked-in seed.
            - |
              curl --silent --location \
                --header "Authorization: Bearer ${BITBUCKET_BOT_TOKEN}" \
                "https://api.bitbucket.org/2.0/repositories/${BITBUCKET_WORKSPACE}/${BITBUCKET_REPO_SLUG}/pipelines/?status=SUCCESSFUL&target.branch=main&pagelen=1" \
                -o latest-main.json
              # In practice you'd parse latest-main.json to find the
              # right artifact URL; for a first pass, ship a seed
              # baseline in the repo and rely on it here.
              cp tests/agent_scenarios/seed_baseline.json "${BASELINE}"
            # 2. Run the benchmark.
            - inkfoot benchmark ./tests/agent_scenarios --output "${CURRENT}" --quiet
            # 3. Diff. Capture exit code so the comment posts before failing.
            - set +e
            - inkfoot diff "${BASELINE}" "${CURRENT}" --format markdown --output cost-diff.md
            - DIFF_EXIT=$?
            - set -e
            # 4. Post the sticky comment.
            - |
              BODY=$(python -c "import sys, json; print(json.dumps(open('cost-diff.md').read()))")
              curl --silent --request POST \
                --header "Authorization: Bearer ${BITBUCKET_BOT_TOKEN}" \
                --header "Content-Type: application/json" \
                --data "{\"content\": {\"raw\": ${BODY}}}" \
                "https://api.bitbucket.org/2.0/repositories/${BITBUCKET_WORKSPACE}/${BITBUCKET_REPO_SLUG}/pullrequests/${BITBUCKET_PR_ID}/comments" \
                > /dev/null
            - exit "${DIFF_EXIT}"
          artifacts:
            - current.json
            - cost-diff.md
```

The Markdown renderer embeds a hidden `<!-- inkfoot-diff-action -->`
marker so the same logic the GitHub Action uses (GET comments,
match on the marker, PATCH instead of POST) can apply here. The
example above always appends a new comment; a small Python helper
or the `pyrepo` Bitbucket SDK can promote it to sticky behaviour.

## Cost guardrails

The same cost guidance applies: filter the pipeline to PRs
that touch agent code (Bitbucket's branch / path triggers), keep
scenarios small, and watch the cost figure printed in the diff.
