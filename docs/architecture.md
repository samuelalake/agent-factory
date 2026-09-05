# Architecture

## Boundary

```text
consumer events
      |
thin caller workflows (pinned factory ref)
      |
factory control plane
  |-- review contract
  |-- triage contract
  |-- builder contract
  `-- priority-ordered gate
      |
consumer adapter
  |-- commands
  |-- protected paths
  |-- evidence collector
  `-- domain skills
```

The factory returns normalized states and evidence. A consumer adapter turns
project-specific commands and artifacts into those states. The dependency
only points inward: factory code never imports a consumer.

## State contract

The gate is a deterministic reducer. Its first applicable rule wins:

1. mergeability;
2. required checks;
3. current-head reviewer freshness;
4. reviewer verdict;
5. P1 findings;
6. untracked P2/P3 findings;
7. success.

This order is tested as a pure function. GitHub API collection and status
publication are adapters around it, not part of the decision logic.

## Distribution

Consumer repositories receive small caller workflows. They pin a factory tag
or commit and pass repository secrets explicitly. Updating a consumer changes
the pin; it does not copy a new generation of orchestration scripts into every
repository.

The public workflow source supports cross-owner callers. Production adopters
should pin a release tag or commit rather than `main`. Package distribution and
an automated update path remain future options.

### App credential modes

The current distribution is self-hosted. One operator may reuse its Builder
and Reviewer Apps across repositories it controls and place their credentials
in organization or repository secrets. An independent adopter creates its own
role Apps (for example, `Acme Builder` and `Acme Reviewer`) from the Factory's
permission contract and owns the corresponding private keys.

Making an App publicly installable does not make its private key distributable.
A globally shared Builder or Reviewer therefore requires a hosted Factory
service that retains the key and mints installation tokens on behalf of each
installation. The Actions-only alpha does not claim that capability.

## Trust boundary

The model produces untrusted JSON. The publisher normalizes its schema and
embeds only a compact, base64-encoded machine contract in the formal review.
The gate independently reads that contract, checks its head SHA against the
current pull request, and applies the pure decision reducer. Human prose is
presentation; it is never parsed as gate state.

## Compatibility strategy

A feature is generic only when multiple repositories can express it through
the configuration, capability, or adapter protocol without adding project
names or domain paths to factory code.

## Project stewardship

Conversation is input, not automatically a queue mutation. A stewardship layer
first updates a human-readable narrative ledger, deduplicates concerns, and
decides whether a concern is ready for durable execution tracking. The ledger
may be backed by a document table, a project view, issues, or a repository file;
the adapter is optional, but a project should name one source of truth.

Triage promotes a ledger concern into an execution item only when it has a
distinct outcome, current evidence, enough context to act, and a useful next
decision or verification step. This keeps the Factory compatible with GitHub
Issues without making issue creation the default response to every message.
