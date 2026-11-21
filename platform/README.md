# platform/

The platform's components, each one an Argo CD Application installed into every cluster of an instance (P1 moves them
here from the reference installation). Rule: **nothing in here names an application**. Everything per application comes
from its `app.yaml` (namespaces, SLOs, synthetic traffic targets, experiment permissions select namespaces by label).

Planned: `monitoring` (kube-prometheus-stack), `pyrra`, `slo` (chart: app.yaml `slos` → Pyrra objectives + Probes),
`blackbox`, `chaos-mesh`, `traffic` (k6, open-loop, day-shaped), `experiments` (daily regression + results exporter),
`cnpg` (optional, for examples with databases), `policies` (optional admission baseline).
