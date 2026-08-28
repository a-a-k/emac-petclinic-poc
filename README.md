# EmaC PetClinic runtime-model discovery artifact

This repository contains a GitHub Actions-only feasibility experiment for EmaC
on the AWS Application Signals fork of Spring PetClinic.

The normative execution is
[`.github/workflows/emac-petclinic-poc.yml`](.github/workflows/emac-petclinic-poc.yml).
By default a dispatch runs one pilot pair only. The confirmatory input explicitly
enables twenty isolated pair-level jobs after a valid pilot; it is never launched
implicitly by a pilot-only dispatch.

The artifact evaluates a narrow discovery mechanism for one supported operator
class. EmaC bootstraps service instances, runtime operators, and executed service
edges from metrics and traces. After hidden circuit-breaker preconditioning, it
discovers the instance-scoped state delta, binds rejected calls to the uniquely
suppressed edge, applies the delta to the bootstrap model, and compiles two
journey-specific estimates before any held-out semantic outcome is sent.

The upstream application source remains unpatched. The runtime deployment adds
experimental configuration, an OpenTelemetry agent, a Spring initializer for
unrelated eager AWS demo beans, deterministic routing and fault injection, and
the discovery/evaluation pipeline.

See [`experiment/README.md`](experiment/README.md) for the executable protocol,
artifact boundaries, validity policy, and exact claim.

The latest retained confirmatory outcome and the digest manifest for all 20 raw
pair artifacts are in [`results/fcb822c`](results/fcb822c/README.md).
