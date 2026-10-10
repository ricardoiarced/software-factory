# Bind Run Admission to Launch Inputs by Value

- **Status:** Accepted
- **Date:** 2026-10-01

## Context

A successful Shared Run Gate authorizes a specific Run. That authorization is meaningful only if the launcher executes the same inputs the gate admitted.

If launch re-derives gated inputs from the filesystem, process environment, identity store, configuration, or other mutable sources after admission, the Run can silently differ from the Run the gate authorized. Ordering alone is therefore insufficient: the admission result needs to become an explicit launch artifact whose values, rather than their backing sources, are authoritative.

Factory represents that artifact as the **Validated Run Plan**. It contains the resolved identity, validated Project Boundary and its proof, Harness identity, effective child environment, argv, exact Instruction and Context bytes, and the admission and evidence verdicts with their provenance. The launcher consumes the plan as its sole source of gated launch inputs.

Some facts are intentionally observed twice. Facts whose change during admission could silently switch the selected boundary or executable are observed while the Validated Run Plan is built and re-observed at the launch boundary. The second observation answers only whether the admitted fact is still the same; it does not contribute new admitted state. Other facts are authoritative exactly once per Run and are never re-read.

This bind-by-value design substantially reduces the mutable-state surface between admission and process creation, but it does not make pathname-based process creation atomic with executable validation. With the current launch recipe, the launcher validates an executable by pathname and later asks the operating system to execute that pathname. The pathname can therefore be replaced in the residual interval between the final observation and the kernel's open for execution.

A held-file-descriptor launch design was considered. On platforms where the validated executable can be executed from the same held descriptor, that design **eliminates pathname-replacement TOCTOU** for the executable: the object validated and the object executed are the same opened object. Adopting that mechanism, however, changes the certified launcher recipe and introduces platform-specific launch mechanics.

Factory's Publication Gate treats such a change as a certified-recipe change requiring recertification.

## Decision

The Shared Run Gate produces an explicit, immutable **Validated Run Plan**.

Admission authorizes the exact launch inputs carried by that plan. The launcher consumes those inputs **by value** and does not re-derive gated state from mutable backing sources after admission.

Plan construction, launch, and outcome remain one in-process chain. Launch consumes the plan directly, and spawn success or failure and the eventual process outcome flow back through that same chain for Run-recording purposes.

The launcher performs the defined Launch Boundary Recheck immediately before process creation. Re-observation is limited to the frozen two-observation fact set. A disagreement refuses the Run as changed during admission; it never silently substitutes the newly observed value into the plan.

Single-observation facts remain authoritative from the plan. Launch does not re-read the identity store, effective configuration, Checkout Git Assessment, publisher lookup, or other sources designated single-observation for that Run.

The remaining executable pathname-reopen window is **accepted residual risk** under the current certified launch recipe. Bind-by-value and the launch-boundary executable recheck narrow that window and prevent post-admission re-derivation, but they do not eliminate pathname-replacement TOCTOU.

Factory reserves the wording **"eliminates pathname-replacement TOCTOU"** for a launch mechanism that validates and executes the same held executable object, such as an appropriate held-fd execution design.

Changing the launcher so that executable validation and consumption are atomic with respect to the executable object is launcher-recipe territory. Such a change is governed by the Publication Gate and requires the corresponding recertification rather than being introduced as an incidental hardening change to Run admission.

## Consequences

A successful gate has a precise meaning: it authorizes the immutable values in one Validated Run Plan, not permission for the launcher to reconstruct an equivalent-looking Run later.

The launcher has a narrow authority surface. It may consume the plan, perform the prescribed launch-boundary comparisons, refuse on disagreement, and create the process. It may not discover replacement gated inputs.

Changes to single-observation backing sources after their authoritative read affect a later Run, not the admitted Run.

Changes to two-observation facts before the launch-boundary recheck cause refusal rather than substitution.

The design gives the Run record a natural provenance chain: admitted values and verdicts originate in the plan, launch outcome is attached to that same plan, and no second admission model needs to be reconstructed after the fact.

The executable path still has a microscopic replacement interval after the final recheck and before execution opens the pathname. The system documents and accepts that residual risk rather than claiming the current recipe closes it.

Closing that interval later is intentionally expensive. Moving to held-object execution changes certified launch mechanics, may require platform-specific implementations, and passes through the Publication Gate and recertification.

## Alternatives considered

### Procedural binding only

The gate could define ordering rules while allowing launch to re-read configuration, environment, identity, paths, or other inputs.

Rejected. Ordering does not establish that execution uses the values admission authorized. Mutable sources can change between the two operations, and later re-derivation obscures which values the gate actually admitted.

### Re-admit everything immediately before spawn

The launcher could perform a second complete gate and launch from the second result.

Rejected. This creates two competing admission authorities, expands the race surface rather than defining it, and weakens the distinction between authoritative single-observation facts and facts that specifically require a same-or-changed boundary check.

### Held-descriptor executable launch now

The launcher could hold the validated executable object and execute that same object rather than reopening its pathname.

This would eliminate pathname-replacement TOCTOU for the executable and is the stronger consumption model.

Not adopted in this decision. It changes certified launch mechanics, introduces platform-dependent implementation concerns, and therefore falls under the Publication Gate. The residual window is accepted until such a recipe change is deliberately proposed and recertified.

## Scope

This ADR establishes the bind-by-value authority model, the relationship between admission and launch, and acceptance of the residual executable pathname-reopen window.

It does not redefine the membership of the two-observation or single-observation sets, the individual refusal diagnostics, the fields of the auditable Run record, or the certified sandbox and launcher recipes. Those remain governed by their respective decisions.
