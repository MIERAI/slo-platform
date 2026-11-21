# Concepts

1. **An SLO measures what a user experiences**, as a ratio `good events / valid events` over a window (default 28
   days). The threshold of a latency SLO is a histogram bucket edge. Client errors (4xx) are valid, not bad, events.
2. **Only an SLO burning fast pages.** Everything else is a ticket or a dashboard.
   | Layer | Fires on | Examples |
   |---|---|---|
   | page | users affected now | fast burn (14× over 1h and 5m), medium burn (7× over 6h and 30m) |
   | ticket | budget eroding slowly, or a safety net broken | slow burn (1d / 4d), SLI absent |
   | dashboard | causes, not symptoms | CPU, restarts, queue depth |
3. **Every alert has been seen firing in an experiment** before anyone relies on it, and normal traffic must not page.
   Experiments run on a lab cluster only; a cluster must say it is a lab before any fault starts.
4. **Measure from outside too.** An in-pod SLI cannot see an outage with no pod left; every user-facing service gets a
   probe SLO, and the alerting pipeline itself is watched from another machine (dead man's switch).
5. **When the error budget is spent, reliability work comes before features**: promotions to prod need a reason below
   25% left; only fixes ship when it is exhausted.
