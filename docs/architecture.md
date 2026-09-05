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
