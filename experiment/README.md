# PetClinic stateful-control-flow PoC

## Fixed claim

> For one predeclared Resilience4j circuit-breaker/fallback operator, EmaC maps
> instance-scoped runtime state and execution evidence to a versioned
> operator-state delta and a journey-specific reliability estimate, which is
> evaluated against a held-out semantic outcome under balanced
> measurement-window local availability SLIs.

This is a mechanism-feasibility PoC. It is not evidence for arbitrary structural
discovery, dependence drift, certification soundness, or operational utility.

## Where it runs

The complete experiment runs in GitHub Actions through
`EmaC PetClinic mechanism-feasibility PoC` (`workflow_dispatch`). The workflow
has no abbreviated local execution branch. Its fixed invocation is:

```text
1 pilot pair
5 valid confirmatory pairs
control/treatment order randomized within each pair
10,000 evidence requests per condition at 100 requests/s
10,000 held-out outcome requests per condition at 100 requests/s
```

An invalid pair is retained and replaced as a whole, up to the predeclared limit
of two replacement pairs. The workflow fails if it cannot obtain five valid
confirmatory pairs. Invalid attempts remain in the uploaded artifacts.

## Isolation of evidence and ground truth

Each condition has this enforced sequence:

```text
restart gateways and reset breaker state
  -> precondition gateway-B
  -> disable fault and verify Visits
  -> direct counter snapshot E0
  -> evidence traffic
  -> direct counter snapshot E1 and trace freeze
  -> evidence-only EmaC evaluation
  -> evidence-freeze.json created
  -> held-out outcome traffic and semantic oracle
```

`emac_evaluate.py` accepts only the declared model and the evidence directory.
That directory contains exact actuator counter snapshots, a status-only load
summary, and normalized/raw traces. It contains no fault schedule, response body,
or oracle result. The runner records and checks that the model freeze predates the
first outcome request.

The independent oracle requires owner `6` and visit `1` for `owner-history`.
For the semantic control `owner-only`, the same empty-visits fallback satisfies
the journey. Thus the same observed `q` lowers the history estimate but leaves
the owner-only estimate at `A_P`.

## Runtime algebra

The evidence adapter computes exact boundary-counter deltas:

```text
N_permitted = successful + failed + ignored permitted calls
N_decision = N_permitted + not-permitted calls
A_P = N_decision / N_eligible
q = N_permitted / N_decision
A_V = successful permitted calls / N_permitted
R_J = A_P [q A_V + (1 - q A_V) A_F]
```

`A_F=0` for `owner-history`; `A_F=1` for `owner-only`. The adapter also verifies
the arithmetic identity `A_P q A_V = successful-permitted / eligible`.

The typed delta contains only the observed runtime-state change:

```text
path: operator[getOwnerDetails].runtimeState[gateway-B]
before: CLOSED
after: OPEN
```

The affected declared edge and activated declared fallback are stored as derived
impacts, not presented as discovered structure.

## Manipulation

Both gateway images are built from the same unchanged upstream commit. The
router sends every 100th request to gateway-B. For treatment preconditioning,
the Visits proxy returns a fast `503` to sequential gateway-B requests. With the
pinned count-based configuration, the nominal record is 100 failed permitted
calls followed by 20 not-permitted calls and final state `OPEN`. Control sends
120 successful requests and remains `CLOSED`. The proxy is disabled and the real
Visits response is checked before measurement starts.

The configuration explicitly pins CircuitBreaker and TimeLimiter properties and
disables the Spring Cloud Resilience4j Bulkhead wrapper. The 15-minute open-state
budget is checked against each condition duration.

The AWS fork constructs DynamoDB, SQS, and Kinesis clients during application
startup. A digest-pinned LocalStack container supplies those APIs inside the
ephemeral Compose network using fixed dummy credentials and the SDK's standard
endpoint override. No request leaves the GitHub runner for an AWS data-plane API.

## Reproducibility bundle

The uploaded `emac-petclinic-poc-*` artifact contains:

- resolved Compose configuration, upstream locks, image locks, dependency tree,
  and container inspection records;
- raw direct Prometheus snapshots at every boundary;
- compressed raw Jaeger traces and normalized instance/edge execution rows;
- the versioned, pre-outcome EmaC model freeze and provenance partition;
- hidden manipulation records and fault/router journals;
- compressed per-request semantic oracle records and outcome summaries;
- every condition and pair validity record, including invalid attempts;
- the aggregate JSON/Markdown report and complete Compose logs.

The manuscript should report all ten valid confirmatory condition-runs, raw
counts, median/range summaries, exact delta recovery, false control deltas, held-
out absolute errors, target-side errors, and local-SLI balance. It should not use
statistical-significance language for this PoC.

## Pinned inputs

- AWS Application Signals demo commit:
  `a6619308ef610c0002ce03eedbaf6672a4fc5cae`
- Experiment Config Server reference snapshot:
  `323993ce2519c6d02df63e08bf4458d123d3b611`
- OTel Java agent: `2.11.0`, SHA-256
  `4cff4ab46179260a61fc0d884f3f170cfbd9d2962dd260be2cff31262d0c7618`
- All container manifest digests: `images.lock.env`
- Circuit-breaker/fallback declaration: `journey-model.json`
- Replication and validity policy: `protocol.json`
