# Confirmatory result for `fcb822c`

The [source workflow run](https://github.com/a-a-k/emac-petclinic-poc/actions/runs/33147282217)
completed successfully with 20 valid paired blocks and no replacements.

- Exact treatment model recovery: 20/20
- False discovery in controls: 0/20
- Metrics-only / traces-only ablations recovered complementary partial facts: 20/20
- Ambiguous / contradictory bindings were refused as unassessable: 20/20 / 20/20
- Trace replay at 10%: 17 recovered, 3 unresolved, 0 false bindings
- Trace replay at 1%: 1 recovered, 19 unresolved, 0 false bindings
- Maximum paired local-availability difference: 0.000 percentage points for Gateway, Customers, and Visits
- EmaC pipeline time excluding trace collection: 0.200 s median, 0.245 s maximum over 40 condition runs

This run includes sealed adapter-catalog lineage, exact reconstruction of each
effective model from its bootstrap model, candidate delta, and reconciliation
decision, and terminal estimate recomputation before freeze and outcome
comparison. [`summary.json`](summary.json) records the exact source commit,
workflow runs, aggregate and detailed results, and a digest manifest for all 20
raw confirmatory artifacts.

