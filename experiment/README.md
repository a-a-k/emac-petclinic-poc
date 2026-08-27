# PetClinic runtime-model discovery PoC

## Fixed claim

> For a pre-supported Resilience4j circuit-breaker operator class, EmaC
> discovers an instance-scoped effective interaction model from runtime metrics
> and traces, binds operator activation to a uniquely suppressed service edge,
> applies a typed model delta, and compiles journey-specific reliability
> estimates without observing journey outcomes.

The PoC does not claim arbitrary architecture discovery, discovery of user
intent, adaptation, certification, or support for operator classes other than
the declared generic Resilience4j adapter.

## Declared versus discovered

The EmaC input contract declares the journey entrypoint and target, a semantic
interaction role expressed as an operation predicate, and whether suppression of
that role satisfies each journey. It does not declare which runtime service edge
will satisfy the role. The generic adapter catalog declares Resilience4j metric
syntax and circuit-breaker semantics.

The following application facts are not EmaC inputs:

- gateway instance identifiers or logical Compose slots;
- the minority/faulted replica;
- the application operator name;
- the downstream service graph;
- the affected edge;
- the initial or current operator state;
- routing weights, fault schedule, response semantics, or oracle result.

The manual dynamic-composite baseline intentionally receives the hand-maintained
operator/edge/fallback mapping. It is not used by the discovery pipeline.

## Executable sequence

Every condition runs in this order:

```text
restart telemetry and both gateways
  -> balanced, evidence-only bootstrap traffic
  -> discover and freeze interaction-model v0
  -> restart both gateways to discard bootstrap breaker history
  -> hidden control/treatment preconditioning
  -> disable the fault and verify Visits directly
  -> evidence counter snapshot E0
  -> evidence traffic with response bodies discarded
  -> counter snapshot E1 and generic trace-graph extraction
  -> discover candidate typed operator-state/edge-binding delta
  -> reconcile as identified, unresolved, or contradictory
  -> apply only an identified delta to v0, producing effective-model v1
  -> compile journey estimates only from v1 + journey contract
  -> freeze model/estimate versions
  -> send new held-out requests and evaluate response semantics
```

The compiler implementation reads only `effective-model.json` and
`journey-contract.json`; filesystem-level process isolation is not claimed. Every
stage recomputes the complete content hash of its inputs before use. The effective
model records its parent, candidate delta, and reconciliation versions; the
compiled estimate records both the effective-model and contract versions.

## Discovery rule

The bootstrap trace graph identifies normally executed outgoing edges per opaque
gateway instance. The runtime adapter enumerates operator names and states from
standard metric labels. For an instance with rejected calls, EmaC considers
bootstrap edges that previously executed on at least 95% of its journey traces.
For every such baseline edge it computes the executions missing from the
post-preconditioning evidence graph on the same opaque instance. It binds the
operator only if exactly one edge satisfies:

```text
missing runtime edge executions ~= not-permitted calls
```

within the predeclared one-percent tolerance. The discovered edge must also
uniquely satisfy the semantic operation predicate required by the journey.
Ambiguous evidence produces a versioned `unresolved` decision; conflicting
evidence produces `contradictory`. Neither delta is applied and compilation
returns `UNASSESSABLE` rather than raising an exception or selecting a candidate.

For example, if bootstrap contains 100/100 Customers and 100/100 Visits edge
executions on an instance, while treatment evidence contains 60/60 Customers,
0/60 Visits, and 60 metric `not-permitted` decisions, only the Visits edge has
`missing executions = not-permitted = 60`.

## Evidence-source ablations and negative control

After the primary paired block freezes its evidence, model, estimate, and
held-out outcome, separate named GitHub Actions steps execute secondary analyses
using only the frozen pre-outcome evidence inputs:

- metrics-only reads counter/state snapshots but never the trace graph; it may
  recover operator identity, state, and `q`, while the affected edge must remain
  unresolved;
- traces-only reads the bootstrap/current trace graphs but never metric
  snapshots; it may recover a uniquely suppressed edge, while operator identity
  and state must remain unresolved;
- full fusion reads both and must produce the typed operator-to-edge delta.

The workflow materializes separate input views: the metrics-only directory has
operator snapshots and the experiment-scoped eligible-request count but no
trace graph, while the traces-only directory has bootstrap/current interaction
graphs but no metric snapshots or operator model. Their manifests and contents
are retained as artifacts.

The negative-case step performs two artifact-level counterfactual replays for
each treatment. The ambiguity replay makes two normally executed bootstrap
edges match the real `not-permitted` count. The contradiction replay makes no
edge match it. Each mutated evidence set passes through the same production
`discover -> reconcile -> apply -> compile` functions. Expected results are an
`unresolved`/`contradictory` decision and `UNASSESSABLE`, respectively. These are
negative evidence replays, not additional live faults.

The robustness step deterministically replays the frozen evidence at 10% and 1%
trace sampling while retaining full operator metrics. It reports correct
recovery, unresolved results, and false bindings separately; fewer than three
sampled traces for the rejected instance force an unresolved result. A second replay
removes instance identity from both evidence families: global `q` must remain
estimable, while the instance-scoped binding must remain unresolved.

## Reliability compilation

The applied model contains:

```text
A_P = operator decisions / eligible requests
q   = permitted decisions / all operator decisions
A_V = successful permitted decisions / permitted decisions
```

For a journey with suppressed-branch semantic value `A_S`:

```text
R_J = A_P [q A_V + (1 - q A_V) A_S]
```

`owner-history` declares `A_S=0`; `owner-only` declares `A_S=1`. The held-out
oracle separately checks owner `6` and visit `1` after the estimate has frozen.

## Randomization and anti-hardcoding

Each pair generates two opaque instance identifiers and randomly selects which
logical gateway is the deterministic one-percent minority. The treatment opens
the breaker on that minority replica; EmaC receives neither mapping. Unit tests
reject the fixture instance names, operator name, and affected downstream name
if they occur in discovery, model-application, compiler, or trace-normalization
source files.

## GitHub execution and durability

The reusable pair job has GitHub's 360-minute maximum. The primary experiment
step has a 300-minute ceiling, and ablations, negative cases, robustness, and
finalization are separate visible workflow steps. Incremental checkpoints and
per-window summaries survive a step timeout.

A pilot uses 200 balanced bootstrap requests plus 2,000 evidence and 2,000
outcome requests per condition at 25 requests/s. A confirmatory pair uses the
same bootstrap and 6,000 evidence/outcome requests per condition at 50
requests/s. Load submission is bounded to 128 in-flight requests, and reports
achieved throughput and latency percentiles rather than only scheduled rate.
Jaeger evidence is queried in ten-second time chunks; every raw response is
immediately written as a separate gzip artifact before its decoded tree is
released, bounding peak collector memory independently of window size. The Java
agent captures `X-Experiment-Run-Id`; normalization admits only entry spans with
the exact window ID and reports rejected adjacent-window traces.

The default workflow dispatch runs only the pilot. Twenty confirmatory pairs are
enabled by an explicit boolean dispatch input and start only after that dispatch's
pilot succeeds. Invalid confirmatory pairs are retained and replaced as whole
pairs, up to two replacements.

## Evidence and artifacts

Each condition publishes:

- direct boundary metric snapshots with opaque identity tags;
- raw compressed Jaeger response chunks and generic normalized edge graph;
- status-only evidence load summary without logical routing identity;
- hash-addressed evidence references, bootstrap model, candidate typed delta,
  reconciliation decision, effective model, and compiled estimate;
- published JSON Schemas and exact discovery/reconciliation/application/compiler
  timing;
- metrics-only, traces-only, and full-fusion reports;
- ambiguity and contradictory-evidence negative-case reports;
- 10%/1% trace-sampling and identity-redaction robustness reports;
- the hand-maintained dynamic-composite baseline;
- hidden runtime assignment, manipulation and routing records;
- held-out per-request semantic decisions and outcome summary;
- version-chain, discovery, trace coverage, local-SLI, and timing checks.

The complete artifact also includes resolved Compose configuration, pinned source
and image inputs, dependency tree, container inspection, checkpoints, and logs.

## Upstream boundary

The workflow checks out pinned commit
`a6619308ef610c0002ce03eedbaf6672a4fc5cae` and verifies a clean upstream worktree
before building. Business source and the existing owner-history fallback remain
unpatched. The deployment does add external configuration, an OpenTelemetry Java
agent, and a separately built Spring initializer that neutralizes unrelated
eager AWS demo beans. This runtime overlay is disclosed and validated in every
artifact.
