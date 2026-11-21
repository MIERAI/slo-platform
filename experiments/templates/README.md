# experiments/templates/

One fault experiment **per SLI type**, instantiated by [`scripts/generate-experiments.py`](../../scripts/generate-experiments.py)
for every SLO an application declares (P6; how to use it: [docs/experiments.md](../../docs/experiments.md#generated-experiments)).
Each file holds notes (above the `---` line: what the template predicts and why) and the experiment (below it), with
`${...}` fields the generator fills from `app.yaml` and the application's manifests. The generator writes a file only
when every field can be derived; otherwise it lists the SLO as skipped, with the reason. A template predicts only
what holds for every application — a prediction that fails on a correct SLO would be a wrong template.

| SLI type | Fault (generic) | Expected | Why not more |
|---|---|---|---|
| `probe` | every pod behind the probed Service fails | the probe SLO's fast burn pages (`long: 1h`) within a bound from the objective | — |
| `availability` | every pod that exports the SLI fails | `SLOMetricAbsent` (ticket) within 14 min (Pyrra: for 6m, + 8); the SLO itself does not page | an in-pod counter cannot count requests that never reach a pod |
| `latency` | every pod that exports the SLI fails | `SLOMetricAbsent` within 18 min (Pyrra: for 10m); the SLO does not page | network delay on the service's own pods is invisible to its in-pod histogram |
| `age` | every pod that exports the timestamp gauge fails | `SLOMetricAbsent` within 14 min | how "the job stops succeeding" is application-specific |
| `ratio` | every pod that exports both counters fails (same pods only) | `SLOMetricAbsent` within 14 min | what makes the errors counter grow is application-specific |
| `bool` | every pod that exports the gauge fails (rarely derivable) | `SLOMetricAbsent` within 14 min | the gauge is usually made elsewhere |

**The pods.** For a probe: the selector of the probed Service (`service:` in the SLO) in the application's manifests.
For the other types: the SLO's equality matchers whose label and value a pod template of the manifests carries (a
`PodMonitor` with `podTargetLabels: [app]` copies the pod label `app` onto the series, so `app="web"` names the pods
`app: web`). Regex matchers, a `url` probe, Helm templates that are not plain YAML, or labels no pod carries: skipped.

**The probe bound.** Every probe fails, so the 1h error ratio grows by 1/60 per minute; Pyrra's fast burn needs 14 ×
the budget: `14 × (100 − objective)% × 60` minutes (99.5%: 4.2 min), then `for: 2m`, plus 4 min of margin for
scrapes, rule evaluation and the runner's polling; at least 8 min. Over 30 min (an objective under ~97.1%) or
impossible (≤ 92.86%): skipped — holding a total outage that long every day is a decision for a hand-written file.

**Why most types predict "SLI absent", not a burn.** A burn needs the SLI itself to count bad events, and a generic
fault cannot make an application count them: with every pod down an in-pod SLI has no data (the reference
installation's first outage experiment paged nobody; `sample-app-down` now expects the probe instead), and delaying a
service's packets does not slow the handler it times (storefront `api-slow-v2/-v3`: `api-latency` quiet; P6's trial
on sample-app: +750ms per packet, the probe took 1.5s, the in-pod p99 stayed at 5ms). What the generic fault proves is still worth a daily run: the SLO's metric
and matchers select live series of those pods (a typo makes the SLI absent all the time: the experiment then fails at
the start, "expected alert already firing"), and losing them raises the ticket. The burn path of such an SLO needs a
fault the application reports — a fault switch (`<app>-faults`, storefront `api-errors`) or a slow dependency
(`api-slow-v3`): hand-written.

Bump `VERSION` in the generator when a template's predictions change; files keep the version they were written with
(`generated: {from, template, version}`), and `--force` rewrites them.
