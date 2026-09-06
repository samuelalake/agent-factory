# Architecture

## Boundary

```text
consumer events
      |
thin caller workflows (pinned factory ref)
      |
factory control plane
  |-- steward contract
  |-- builder harness
  |-- review contract
  |-- integration contract
  `-- priority-ordered gate and landing policy
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

## Role and automation boundary

Steward owns the work state and the transition decision. Builder owns source
changes. Reviewer owns the independent current-head judgment. None of those
roles directly implements the deterministic mechanisms that prove or land a
change.

```text
Steward dispatch
      |
Builder branch and pull request
      |
repository Verify + independent Reviewer
      |
Steward integration decision
      |
deterministic integration status + merge queue or landing job
```

The default integration lane is the pull request's synthetic merge ref: it
tests the current head combined with its base without maintaining a drifting
long-lived branch. Consumers may configure a branch-backed development lane
when they genuinely need several reviewed changes deployed together. Human
acceptance is a repository policy for selected risk classes, not a mandatory
hold on every change.

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

The current distribution is self-hosted. One operator may reuse its Steward,
Builder, and Reviewer Apps across repositories it controls and place their credentials
in organization or repository secrets. An independent adopter creates its own
role Apps (for example, `Acme Builder` and `Acme Reviewer`) from the Factory's
permission contract and owns the corresponding private keys.

Making an App publicly installable does not make its private key distributable.
A globally shared Builder or Reviewer therefore requires a hosted Factory
service that retains the key and mints installation tokens on behalf of each
installation. The Actions-only alpha does not claim that capability.

## Trust boundary

The model produces untrusted output and tool requests. Builder runs in an
ephemeral repository checkout without GitHub App credentials in the model tool
environment. The harness owns commits, pushes, and pull-request publication.
Gemini CLI execution is time-bounded and emits auditable tool events. NVIDIA
fallback execution additionally caps model requests, confines file tools to the
repository, blocks publication and credential-inspection commands, and returns
tool failures to the model as data.

Reviewer output is untrusted JSON. The publisher normalizes its schema and
embeds only a compact, base64-encoded machine contract in the formal review.
The gate independently reads that contract, checks its head SHA against the
current pull request, and applies the pure decision reducer. Human prose is
presentation; it is never parsed as gate state.

## Compatibility strategy

A feature is generic only when multiple repositories can express it through
the configuration, capability, or adapter protocol without adding project
names or domain paths to factory code.

## Project stewardship

Conversation is input, not automatically a queue mutation. Steward
first updates a human-readable narrative ledger, deduplicates concerns, and
decides whether a concern is ready for durable execution tracking. The ledger
may be backed by a document table, a project view, issues, or a repository file;
the adapter is optional, but a project should name one source of truth.

Steward promotes a ledger concern into an execution item only when it has a
distinct outcome, current evidence, enough context to act, and a useful next
decision or verification step. This keeps the Factory compatible with GitHub
Issues without making issue creation the default response to every message.
