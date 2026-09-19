---
name: product-design-delivery
description: Shape, explore, approve, hand off, or review a user-facing product change against the adopting product's real design system. Use when work needs a design decision, an existing design must be reproduced faithfully, or Builder needs an approved interaction and visual contract before implementation. Do not use for purely internal changes with no user-facing behavior.
---

# Product design delivery

Move a user-facing change from intent to an approved, implementable design
without inventing product direction or treating implementation output as its
own specification.

The adopting product owns its design system, platform conventions, artifact
locations, approval authority, and verification tools. Discover and follow
that local context. This skill supplies the decision process and handoff
contract, not a universal visual style.

## Establish the authority

Before proposing a design, inspect the current product and the sources the
repository identifies as authoritative. These may include shipped behavior,
Figma, Claude Design, an Origami or motion prototype, screenshots, component
catalogs, design tokens, code, decision records, and prior user feedback.

Name the authority in the task. When sources disagree, do not silently choose
one. Surface the conflict and identify the decision needed.

Do not infer that an artifact is approved, current, shipped, or canonical from
its existence. Preserve the distinction between a concept, prototype,
reconstruction, approved design, implemented change, and verified release.

## Classify the work

Choose the mode that best describes the requested outcome:

- **Reproduce:** an authoritative design already exists. Preserve its
  composition and behavior; do not redesign it under the guise of improvement.
- **Extend:** add a state or flow by reusing established components and
  conventions. Prefer existing product grammar over a novel direction.
- **Design:** resolve a new product or interaction decision. Explore enough
  alternatives to expose the meaningful tradeoff, then converge.
- **Systemize:** create or change a reusable component, token, or pattern.
  Evaluate its family of states and downstream consumers, not only the first
  screen that needs it.

A task may move from Design to Systemize, but avoid expanding a bounded
Reproduce or Extend request without approval.

## Decide whether design is blocking Build

Design work is required before Build when an unresolved choice would
materially change layout, interaction, product behavior, content hierarchy,
or a reusable system rule.

Design work is not automatically required when:

- the authoritative artifact already specifies the requested behavior;
- the change is a faithful implementation or repair;
- the product gives Builder explicit, bounded design latitude; or
- the decision is an ordinary application of an established component.

When design is required, keep the item out of the build-ready queue until the
design authority approves the design contract. An agent may recommend a
direction but must not approve its own proposal. Design approval does not by
itself authorize unrelated implementation, publication, or release actions.

## Explore at the right fidelity

Start with the cheapest artifact that can answer the open question. Use flows
or wireframes for structure, interactive prototypes for behavior, and
high-fidelity compositions for visual decisions. Do not polish a direction
whose product logic is still unsettled.

When alternatives are useful, make them meaningfully different and explain
the tradeoff each tests. Stop generating options after a direction is chosen.
Carry the approved decisions forward instead of restarting from a blank canvas.

Keep distinct review passes when that improves judgment:

- product outcome and information hierarchy;
- layout, grouping, spacing, and responsive behavior;
- interaction, motion, and state transitions;
- content and voice;
- visual finish and system consistency.

Show the artifact before implementation at the decisions where visual or
interaction judgment matters. A textual description alone is not design
approval when the decision is inherently visual.

## Use the product's system

Reuse real components, tokens, assets, platform behavior, and naming. Inspect
their implemented states rather than relying only on a catalog thumbnail.
Account for relevant empty, loading, error, success, disabled, selected,
permission, interruption, and recovery states.

Treat motion as behavior: specify trigger, transition, continuity, completion,
interruption, and reduced-motion behavior where relevant. Treat accessibility
and platform conventions as part of the design rather than post-build cleanup.

Choose tools by the durable outcome:

- use a fast design canvas or interactive prototype to explore and gain
  alignment;
- use the product's canonical design tool for lasting system decisions and
  editable product artifacts;
- use the supplied source artifact for fidelity work;
- use code as the design medium when it is the clearest faithful expression of
  the interaction.

Do not translate an approved artifact into another tool merely to satisfy a
preferred workflow. Record where the authoritative result lives.

## Produce a design-ready task

Before handoff, create or validate the packet in
[references/design-ready-task.md](references/design-ready-task.md). Keep it in
the project's configured source of truth and link rather than duplicate large
artifacts.

The packet must make clear:

- the user outcome and bounded scope;
- the work mode and authoritative references;
- the approved composition, behavior, and relevant states;
- existing system elements to reuse;
- what must be exact, what Builder may adapt, and what is excluded;
- implementation constraints already known;
- the evidence required to judge the delivered result;
- approval state and any remaining human decision.

If a material decision remains open, return the item to design rather than
writing acceptance criteria that conceal the ambiguity.

## Handoff and review

Builder implements the approved contract and may surface constraints or
propose a bounded amendment. Builder must not silently substitute a different
product decision. Record approved amendments in the task so Reviewer judges
the current contract rather than an obsolete artifact.

Bind delivery evidence to the exact version under review. Use the correct
device, viewport, content, and environment. Screenshots prove states;
recordings prove transitions and interaction. Prefer a compact set that shows
the requested behavior over repeated or unrelated media.

Review both system consistency and perceptual fidelity. Metrics, snapshots,
tests, and successful builds are evidence, not substitutes for visual judgment.
When a material mismatch remains, describe it concretely and return it to
Builder or Design according to whether the cause is implementation or an
unresolved product decision.

## Preserve project control

Keep observations and alternatives in the project's intake or design view.
Promote only approved, bounded work into the executable issue queue. Update an
existing concern when possible instead of creating a new issue for every
comment or iteration.

Do not broaden scope, create external artifacts, publish designs, dispatch
Builder, or mark design approved unless the current task and project authority
permit that action.
