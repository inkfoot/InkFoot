# cost-smells

The open catalogue of **cost smells** — named patterns in an AI agent
run's token attribution that almost always mean wasted money. Each smell
is one YAML file describing what the pattern is, how to detect it, and
how to fix it.

This catalogue feeds two places:

- **[library.inkfoot.dev](https://library.inkfoot.dev)** renders one
  browsable page per smell.
- The **`inkfoot` package** bundles a snapshot of this catalogue at
  release time, so smell detection works offline and refreshes when you
  upgrade the package.

## Layout

| Path | What it is |
|---|---|
| [`schema/smell.schema.json`](schema/smell.schema.json) | The frozen v1 schema every smell file must satisfy. |
| [`smells/`](smells/) | One YAML file per smell. |
| [`fixtures/`](fixtures/) | Positive/negative example runs per smell (the publish bar). |
| [`tools/validate_smells.py`](tools/validate_smells.py) | The validator the lint bot runs on every PR. |
| [`site/`](site/) | The static-site generator for library.inkfoot.dev. |

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md). In short: add one YAML file
under `smells/`, ship at least 3 positive and 3 negative fixtures, make
the recommendation a single actionable sentence, and keep the detection
query to a single linear pass. Validate locally first:

```bash
pip install pyyaml jsonschema
python tools/validate_smells.py
```

## The schema is frozen

The v1 schema does not change shape. Two fields — `estimated_savings`
and `evidence_kind` — are **reserved**: they are written only by the
savings-estimation worker, never by a contributor, and are absent until
there is measured evidence. Any future schema change must be a new
*optional* field, so existing community files never need editing.

## License

[Apache 2.0](LICENSE).
