<!--
Thanks for contributing to Inkfoot! Keep the description focused on the
"why" — the diff already shows the "what". Link the issue this closes.
-->

## Summary

<!-- What does this change do, and why? 1–3 sentences. -->

Closes #

## Type of change

- [ ] Bug fix (non-breaking change that fixes an issue)
- [ ] New feature (non-breaking change that adds capability)
- [ ] Breaking change (changes existing behaviour or public API)
- [ ] Docs only
- [ ] Provider / LangChain coverage (new or fixed capture)

## Checklist

- [ ] Tests pass locally (`pytest tests/unit tests/property tests/docs`).
- [ ] Linters and type checks are clean (`ruff check .`, `ruff format --check .`, `mypy inkfoot`).
- [ ] New or changed behaviour has tests, including edge cases (empty input, boundaries, error paths).
- [ ] A bug fix includes a regression test that fails without the fix.
- [ ] New instrumentation hooks are wrapped so a hook that raises cannot break the host call.
- [ ] Docs are updated for any user-visible change, and the strict docs build passes.
- [ ] `CHANGELOG.md` has an entry under the unreleased section if the change is user-visible.

## Notes for reviewers

<!-- Anything that needs context: trade-offs, follow-ups, areas to scrutinise. -->
