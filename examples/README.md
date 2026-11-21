# examples/

- `sample-app/` — the quickstart: one HTTP service, availability / latency / probe SLOs, canary releases.
- `storefront/` — the reference: web → api → Postgres → worker. Dependency chain and asynchronous pipeline SLIs
  (intake, freshness, completeness), the full experiment set. Not installed by default.
