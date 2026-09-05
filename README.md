# Agent Factory

Agent Factory is a repository-native control plane for software agents. It
gives specialized GitHub Apps a shared way to discover project context, use
skills, exchange evidence, and move work from issue to reviewed change without
hardcoding how any one codebase works.

The project grew out of the agent loop developed in
[Swami](https://github.com/swamikit/swami). The reusable orchestration belongs
here; project-specific judgment stays with the project that owns it.

## The model

Agent Factory separates three concerns:

- **Knowledge:** repository guidance, decisions, skills, commands, and evidence
  standards that agents discover for the task at hand.
- **Agency:** role-specific agents for triage, implementation, and review, each
  operating with the tools and least privilege its role requires.
- **Policy:** deterministic state transitions and merge gates that validate
  agent output without trusting model-authored prose.

## How work moves

The factory coordinates a staged loop rather than one all-powerful bot:

```text
Intent
  → Triage: understand, de-duplicate, and route
  → Build: inspect, plan, implement, and self-check
  → Verify: run deterministic tests and collect evidence
  → Review: evaluate the current head against context and evidence
  → Gate: compute merge readiness from trusted state
  → Land and learn: publish, record decisions, and improve repository context
```

Build and Review may cycle several times. Verify is not a model opinion: it is
the repository's executable evidence. Gate is not another agent: it is a
deterministic policy reducer. The role Apps make each handoff and authority
visible without forcing every project into the same implementation procedure.

The factory owns:

- event and state contracts for review, triage, build, and merge gating;
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

## Status

Agent Factory is a public alpha. The current implementation includes a
versioned configuration contract, pure priority-ordered gate, GitHub
review/gate adapters, machine-readable review artifacts, short-lived GitHub
App authentication, reusable caller workflows, and an idempotent installer.

Triage and general implementation agents are the next runtime boundary. The
alpha should be evaluated in trusted repositories before it is treated as a
hands-off production system.

## Quick start

```bash
python3 -m pip install -e .
agent-factory init /path/to/repository --factory-ref main
python3 -m unittest discover -s tests -v
```

The installer creates `.agent-factory/config.json` and thin workflows under
`.github/workflows/`. It preserves existing files unless `--force` is
explicit.

The [`skills/`](skills/) directory includes two optional starting points for
adopters: project stewardship that prevents issue-sprawl, and human-facing
writing. They are intentionally small. Domain judgment and project-specific
verification still belong in each adopting repository.

The runtime is model-agnostic: a role chooses a provider and model through
versioned configuration, while its workflow receives a generic model
credential. Provider choice does not change the review or gate contract. The
included adapters for Anthropic, Gemini, and OpenRouter let hosted frontier
models and inexpensive/free API tiers participate without redesigning the
loop; local and other compatible runtimes can implement the same boundary.

Role workflows also receive `AGENT_FACTORY_APP_ID` and
`AGENT_FACTORY_APP_PRIVATE_KEY`. They mint short-lived installation tokens so
agent-authored work has a distinct App identity and never relies on a
long-lived personal token. The gate fails closed unless it finds a current-head
approval carrying the factory's machine-readable review contract.

## Design rule

The factory may define what a current, approved, evidence-backed change means.
It must not encode how a particular product is built, tested, rendered, or
deployed. Those capabilities are discovered from the adopting repository.

Read [Architecture](docs/architecture.md) for the system boundary and
[The repository should brief the agent](docs/the-repository-should-brief-the-agent.md)
for the project thesis.
