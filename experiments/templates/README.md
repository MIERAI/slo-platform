# experiments/templates/

Fault experiments **per SLI type**, instantiated for every SLO an application declares (P6). Each one states its
hypothesis and predictions before it runs: which burn-rate alert fires within how long, which alerts must stay quiet,
and an `effect` query proving the fault actually bites. A wrong prediction is a finding, never edited to pass.

| SLI type | Fault | Expected |
|---|---|---|
| availability | the service returns errors / its pods fail | fast-burn page within ~10 min |
| latency | network delay to the service or its dependency | latency page; availability stays quiet |
| probe | every pod down (nothing left to count in-pod) | probe page |
| age | the job stops succeeding | freshness ticket / page by severity |
| ratio | the consumer stops / slows | completeness / freshness page |
| (negative) | normal traffic for hours | nothing pages |
