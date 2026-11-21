# Changelog

All notable changes; the format follows [Keep a Changelog](https://keepachangelog.com/), versions follow SemVer.

## [Unreleased]

### Added
- Platform components (P1): monitoring with default alert routing (only prod notifies, inhibition), Pyrra, the `slo`
  chart (6 SLI types, probe url/prober, per-namespace experiment-runner Role), blackbox, Chaos Mesh, `traffic` chart
  (k6 per app × environment from `app.yaml` `traffic`, real day aligned to the clock), daily experiment regression,
  cert-manager, Argo Rollouts, CloudNativePG; optional: Kyverno workload baseline, in-cluster object storage.
- ApplicationSet: every `examples/*/app.yaml` → Applications; namespaces labelled `slo-platform/app`, `slo-platform/env`.
- Examples: `sample-app` (canary releases), `storefront` (dependency chain, asynchronous pipeline, 8 SLOs).
- Experiment tooling: `experiment.py` (`--prometheus`, `--context`, runs on the same day append instead of
  overwriting), `experiment-regression.py` (node label `slo-platform/lab=true` instead of a cluster name),
  `scorecard.py`, `alert-report.py`; 14 example experiments; `scripts/test.sh`, `scripts/validate.sh`.
- Docs: `docs/app-yaml.md`, `docs/experiments.md`.
- Repository layout, license (Apache-2.0), contribution and security policies, CI skeleton, `platform.example.yaml`
  (configuration draft).
