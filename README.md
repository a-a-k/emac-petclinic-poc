# EmaC PetClinic mechanism-feasibility artifact

This repository contains a GitHub Actions-only mechanism-feasibility experiment
for EmaC on the AWS Application Signals fork of Spring PetClinic.

The normative execution is [.github/workflows/emac-petclinic-poc.yml](.github/workflows/emac-petclinic-poc.yml).
It performs one pilot pair and five confirmatory control/treatment pairs on an
ephemeral GitHub-hosted runner, then publishes the complete reproducibility
bundle as a workflow artifact. No CloudWatch account or local experiment run is
required.

The experiment is deliberately narrow: one predeclared Resilience4j
`getOwnerDetails` circuit-breaker/fallback operator. Metrics identify its
instance-scoped runtime state and decision counts; traces corroborate suppression
of the declared Gateway-to-Visits edge. EmaC freezes its delta and estimate before
the held-out oracle sends any outcome request.

See [experiment/README.md](experiment/README.md) for the executable protocol,
artifact layout, validity policy, and exact claim boundary.
