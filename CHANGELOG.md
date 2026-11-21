# Changelog

All notable changes; the format follows [Keep a Changelog](https://keepachangelog.com/), versions follow SemVer.

## [Unreleased]

### Added
- The instance model (P2): your repository holds `platform.yaml` (apiVersion slo-platform/v1) and `apps/`; the platform
  comes from this repository at `product.version`. Root chart `platform/root` (values = platform.yaml + `cluster`;
  roles prod / lab; self-managed root Application; time zones with their standard offset), replacing `apps/`.
- `bin/slo-platform`: `init` (instance repository, examples, optional age/SOPS; `.tofu/` and `.secrets/` owner-only),
  `check` (platform.yaml schema, app.yaml, experiments reference declared SLOs), `upgrade --version`.
- `bootstrap/tofu`: module (Argo CD, repository credentials, Grafana admin, generic Secrets, root Application) and the
  roots `kind` (labels lab nodes) and `cluster` (existing cluster, explicit context). `docs/configuration.md`.

### Changed
- Grafana's admin login comes from a Secret (no fixed default password); the daily regression only on lab clusters,
  cloning the product at its version and the instance's experiments.
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
