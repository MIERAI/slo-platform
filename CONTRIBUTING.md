# Contributing

The project is an alpha maintained on a best-effort basis (README, "Project status and support"): issues, ideas and
pull requests are welcome. Start with an issue for anything larger than a fix, so we agree on the approach first.

- **Principles first.** Changes follow [docs/concepts.md](docs/concepts.md): symptoms page, causes do not; every alert
  is tested; predictions of an experiment are never edited to make it pass (write a new experiment instead).
- **Nothing personal in the product.** No addresses, hostnames, organisations or time zones in platform code: they
  belong in `platform.yaml`. CI rejects some of them (`scripts/check-residue.sh`).
- **Commits**: one topic each, the message says why. CI must be green.
- **Faults only on lab clusters.** Never point an experiment at a production cluster, even to try it.
