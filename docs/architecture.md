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

GitHub does not make a private personal repository's reusable workflows
available to repositories owned by another account. The alpha therefore keeps
distribution as an explicit launch decision: either publish and license the
workflow source, or move the executable runtime to an authenticated package
channel. Swami must not be pointed at a private cross-owner workflow that can
never resolve.

## Trust boundary

The model produces untrusted JSON. The publisher normalizes its schema and
embeds only a compact, base64-encoded machine contract in the formal review.
The gate independently reads that contract, checks its head SHA against the
current pull request, and applies the pure decision reducer. Human prose is
presentation; it is never parsed as gate state.

## Compatibility strategy

Swami remains the reference fixture until a second consumer proves the
abstractions. A feature is generic only after it can be expressed through the
consumer configuration or adapter protocol without mentioning Swami paths,
Origami, SwiftUI, or the pixel oracle.
