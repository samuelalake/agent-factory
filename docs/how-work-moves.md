# How Work Moves

Agent Factory coordinates a staged loop rather than one all-powerful bot:

```text
Intent
  → Steward: understand, de-duplicate, qualify, and dispatch
  → Builder: inspect, plan, implement, and self-check
  → Figma Writer: mutate and verify the canvas under a Steward-owned lease when configured
  → Verify: run deterministic tests and collect evidence
  → Reviewer: evaluate the current head against context and evidence
  → Gate: compute merge readiness from trusted state
  → Integrate: test the combined state in a preview or development lane
  → Steward: land by policy and route follow-up work
```

Build and Review may cycle several times. Integration failures return to
Builder rather than silently weakening the release. Verify is not a model
opinion: it is the repository's executable evidence. Gate is not another agent:
it is a deterministic policy reducer. The role Apps make each handoff and
authority visible without forcing every project into the same implementation
procedure.

Each dispatch carries a bounded acceptance contract: intended outcome,
in-scope behavior and artifacts, required evidence, and material non-goals.
Reviewer checks the delivered head against that contract and durable repository
decisions. It may identify concrete defects in changed behavior, but it does not
invent product direction, propose alternative architecture, expand the issue,
or require optional polish. Those observations return to Steward's ledger for
human triage. A review cycle exists to verify a correction, not to search for a
new interpretation of the task.

Figma Writer is a specialized delivery phase rather than a second orchestrator.
Builder owns repository implementation; Figma Writer owns only native canvas
mutation and its durable delivery record. Different issues may build in
parallel, but their Figma phases queue on the configured OAuth-identity lease.
Per-issue delivery concurrency prevents a revision Builder from racing the prior
Figma phase on the same branch.

## Delivery evidence

Builder's pull-request description is the canonical delivery record. Consumers
that enable `review.require_builder_delivery` receive a current-head delivery
section with an explicit `pending`, `ready`, or `failed` state. Their trusted
repository verification publishes documentation, media, and test evidence into
that section with Builder's App token. Reviewer waits for the section and fails
closed on missing, stale, or failed evidence; a link or green workflow by itself
is never treated as proof. Repository-specific rendering and interaction logic
remain in the consumer.

## Evidence disputes

When authenticated Reviewer runs materially contradict one another about the
same unchanged reference digest, Factory treats that as an exceptional evidence
dispute rather than another Builder revision. Steward receives the labeled,
current-head Builder images and the authenticated review history, then may
publish one ruling bound to the repository, pull request, exact head, and
reference digest. Reviewer reruns once with that ruling as visual continuity;
it still owns code findings and approval. Arbitration never dispatches Builder,
and a missing, unreadable, unresolved, or non-visual route fails closed. Set
`steward.arbitration_provider`, `arbitration_model`, and
`arbitration_visual_evidence` explicitly; an optional arbitration fallback has
its own provider, model, and visual-capability flag.

## Media publication

Media publication uses GitHub CLI 2.99 or newer's supported `--attach` flow.
The CLI rewrites local references inside the canonical delivery section to
GitHub-hosted user-attachment URLs: screenshots render inline and a standalone
video reference renders as GitHub's native player. GitHub CLI does not accept a
GitHub App installation token for attachment uploads, so Factory uses the
separate `AGENT_FACTORY_MEDIA_UPLOAD_TOKEN` only to create a uniquely marked
staging comment, capture the durable URLs, and delete that comment. Factory then
re-reads the exact head and latest pull-request body before the Builder App makes
the canonical delivery write, so a slow or partial upload cannot overwrite a
newer Builder or human revision.

The Builder App also maintains collapsible provenance records per source head.
Per-head records prevent a slow stale publisher from overwriting newer proof;
the canonical body selects its exact matching record, so overlapping same-head
runs cannot deadlock. Each manifest binds the repository, pull request, exact
head, ordered native URLs, media types, and SHA-256 digests; Reviewer and a
revising Builder reject copied, conflicting, or byte-mismatched evidence. No
evidence branch is required. Factory preflights all media, rejects duplicates,
and uses GitHub's portable 10 MB limit per image or video.

## Ownership boundary

The factory owns:

- event and state contracts for stewardship, build, review, integration, and merge gating;
- discovery and loading of relevant repository context and skills;
- safe review publication and fail-closed degradation;
- role-specific GitHub App authentication;
- model-provider adapters behind provider-neutral role contracts;
- installation and versioned updates of thin caller workflows;
- fixture-driven contract tests.

Each adopting repository owns:

- build, test, and evidence commands;
- protected paths and domain-specific verification;
- its instructions, decisions, context, and skills;
- deployment and product policy.
