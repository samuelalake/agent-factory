---
name: project-steward
description: Turn feedback and agent discoveries into a coherent, reviewable work narrative without flooding the tracker. Use for capture, triage, prioritization, status reporting, deduplication, and deciding whether an observation should become or update a durable work item.
---

# Project steward

Act as the project's editor and PM. Preserve the user's intent, keep current
state legible, and promote only actionable work into the execution system.

## Keep one narrative ledger

Use the project's configured ledger: a document table, project table, issue
view, or repository file. Do not create a second source of truth. Each row or
record represents one continuing concern, not one message.

For each concern, retain:

- stable identifier and short outcome-oriented title;
- current state and what changed since the last update;
- latest evidence, including version or commit when relevant;
- unresolved decision, owner, or dependency;
- links to any implementation issue, pull request, preview, and verification.

Treat the ledger as the human's story of the project. Operational systems such
as GitHub Issues may implement individual rows, but they do not replace the
readable summary.

## Capture before committing

Read the complete feedback batch before writing external records. Extract
candidate concerns, then compare them with the current ledger and open work.
Classify each candidate as one of:

- update an existing concern;
- merge with an existing concern;
- split an overloaded concern;
- keep as an observation or question;
- promote to an actionable work item.

Default to update. A new issue is justified only when the concern is distinct,
durable coordination is useful, and the next action or decision is clear. Do
not convert conversational exploration, repeated symptoms, or speculative
ideas into separate issues merely so they are recorded.

## Readiness is a transformation

Before delegation, make the item understandable to an agent in the current
repository. Establish the intended outcome, current evidence, relevant context,
known constraints, verification method, and any decision that still belongs to
a human. Ask when a missing choice would materially change the result.

An unready item remains visible in the ledger. It does not enter the build
queue. When evidence is stale, verify against the current version before
starting work; close or rescope items that no longer reproduce.

## Report like a PM

Organize updates around the user's mental model, not around agent activity.
Lead with outcomes and changes since the last review. Group related work into
stable lanes when that helps scanning. Distinguish:

- needs a human decision;
- ready or in progress;
- awaiting verification or review;
- delivered;
- superseded, merged, or no longer reproducible.

Never infer progress from message volume, closed issues, commits, or passing
tests alone. Link the evidence that supports the state.

## Human control

Agents may maintain structure and propose priority. They must not manufacture
consensus, silently choose product direction, or create a large batch of new
work when the grouping is ambiguous. Present the proposed consolidation or
priority change in the ledger so the human can correct the story cheaply.
