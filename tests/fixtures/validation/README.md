# Validation corpus

Hand-labelled corpus consumed by `scripts/validate_attribution.py`.
Each `<name>.json` carries `provider`, `model`, `request`, `response`. `labels.json` (or `labels.yaml`) maps fixture filename to expected per-category counts.

real-world coverage target is ≥ 50 runs across both providers and ≥ 4 models per provider. The current corpus is a starter set generated synthetically; the team grows it by labelling real provider traffic.

Run the harness locally with::

    python scripts/validate_attribution.py
