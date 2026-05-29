<!-- inkfoot-diff-action -->
## Inkfoot cost diff · ❌ fail

_Thresholds preset: **default** · baseline 2026-05-25T12:00:00Z → current 2026-05-25T12:00:00Z_

| Scenario | p50 Δ | p95 Δ | cache hit Δ | LLM calls Δ | Verdict |
|---|---|---|---|---|---|
| customer-support-triage | +51.2% | +50.6% | 0.0pp | 0.00 | ❌ fail |
| email-summary | +10.0% | +5.0% | 0.0pp | 0.00 | ⚠️ warn |

### Regressions

- **customer-support-triage** — ❌ fail
  - p50 cost regressed by 51.2% (fail threshold +50.0%)
  - p95 cost regressed by 50.6% (fail threshold +50.0%)
- **email-summary** — ⚠️ warn
  - smell 'unstable-prompt-prefix' appeared (0 -> 2)

### Smell changes

- **email-summary** — `unstable-prompt-prefix`: appeared (2 runs affected)

_Inkfoot baseline `1.0.0` → current `1.0.0`._
