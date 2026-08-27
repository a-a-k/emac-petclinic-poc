# Confirmatory result for `c0c1826`

The [source workflow run](https://github.com/a-a-k/emac-petclinic-poc/actions/runs/33099612734)
completed successfully with 20 valid paired blocks and no replacements.

- Exact treatment model recovery: 20/20
- False discovery in controls: 0/20
- Metrics-only / traces-only ablations recovered complementary partial facts: 20/20
- Ambiguous / contradictory bindings were refused as unassessable: 20/20 / 20/20
- Trace replay at 10%: 19 recovered, 1 unresolved, 0 false bindings
- Trace replay at 1%: 2 recovered, 18 unresolved, 0 false bindings
- Maximum paired local-availability difference: 0.000 percentage points for Gateway, Customers, and Visits
- EmaC pipeline time excluding trace collection: 0.198 s median, 0.237 s maximum over 40 condition runs

[`summary.json`](summary.json) records the exact source commit, workflow and
artifact identifiers, artifact digests, primary results, and timing scope. Raw
paired evidence remains attached to the source workflow run; the compact summary
is committed so the reported result does not disappear with artifact retention.
