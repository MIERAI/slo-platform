# Contributing

The project is pre-alpha and developed in the open later; issues and ideas are welcome now.

- **Principles first.** Changes follow [docs/concepts.md](docs/concepts.md): symptoms page, causes do not; every alert
  is tested; predictions of an experiment are never edited to make it pass (write a new experiment instead).
- **Nothing personal in the product.** No addresses, hostnames, organisations or time zones in platform code: they
  belong in `platform.yaml`. CI rejects some of them (`scripts/check-residue.sh`).
- **Commits**: one topic each, the message says why. CI must be green.
- **Faults only on lab clusters.** Never point an experiment at a production cluster, even to try it.
