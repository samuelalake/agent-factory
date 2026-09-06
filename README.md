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
- **Agency:** role-specific agents for stewardship, implementation, and review, each
  operating with the tools and least privilege its role requires.
- **Policy:** deterministic state transitions and merge gates that validate
  agent output without trusting model-authored prose.

## How work moves

The factory coordinates a staged loop rather than one all-powerful bot:

```text
Intent
  → Steward: understand, de-duplicate, qualify, and dispatch
  → Builder: inspect, plan, implement, and self-check
  → Verify: run deterministic tests and collect evidence
  → Reviewer: evaluate the current head against context and evidence
  → Gate: compute merge readiness from trusted state
  → Integrate: test the combined state in a preview or development lane
  → Land and learn: promote by policy and improve repository context
```

Build and Review may cycle several times. Integration failures return to
Builder rather than silently weakening the release. Verify is not a model opinion: it is
the repository's executable evidence. Gate is not another agent: it is a
deterministic policy reducer. The role Apps make each handoff and authority
visible without forcing every project into the same implementation procedure.

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

## Status

Agent Factory is a public alpha. The implementation includes a versioned
configuration contract; executable Steward, Builder, and Reviewer roles;
current-head machine contracts; a pure priority-ordered gate; a deterministic
integration/landing adapter; short-lived GitHub App authentication; reusable
caller workflows; and an idempotent installer.

Builder uses a pinned Gemini CLI headless harness first and can fall back to a
bounded NVIDIA Kimi tool loop. Model credentials are withheld from publication
steps, GitHub credentials are withheld from model tool environments, and the
fallback refuses to mix with a partially modified primary workspace. This is
still an alpha: dispatch labels should be restricted to trusted maintainers and
the workflows should be evaluated in trusted repositories before hands-off use.

## Quick start

```bash
python3 -m pip install -e .
agent-factory init /path/to/repository --factory-ref main
python3 -m unittest discover -s tests -v
```

The installer creates `.agent-factory/config.json` and thin Steward, Builder,
Reviewer, and Gate workflows under `.github/workflows/`. It preserves existing
files unless `--force` is explicit. The generated Builder caller uses
`ubuntu-latest`; a consumer that requires a different toolchain changes the
caller's `runner` input while keeping the role contract unchanged.

`project.context_files` names the repository's durable guidance.
`project.skill_dirs` names local skill catalogs; each role selects a bounded
set whose name and description match the current task. The adopting repository
therefore controls both the knowledge and the procedures a role receives.

The [`skills/`](skills/) directory includes two optional starting points for
adopters: project stewardship that prevents issue-sprawl, and human-facing
writing. They are intentionally small. Domain judgment and project-specific
verification still belong in each adopting repository.

The runtime is model-agnostic: a role chooses a primary harness/provider and an
optional fallback pair through versioned configuration. Provider choice does
not change the review or gate contract. Builder supports Gemini CLI plus bounded
OpenAI-compatible loops for MiniMax, NVIDIA, and OpenRouter; Reviewer also
supports Anthropic. The generated configuration starts with Gemini and falls
back to NVIDIA Kimi; both are quota-limited services, so the delivery record
names the provider and model that actually served the run instead of implying
that a free tier is unlimited.

Builder configuration can also set `max_model_requests`,
`max_model_cost_usd`, `input_cost_per_million`, and
`output_cost_per_million`. The cost ceiling is calculated from provider-reported
token usage. Keep a provider-side account or key budget as the authoritative
hard stop because a response is billed before its usage can be evaluated.

Caller workflows pass provider-specific secrets such as `GEMINI_API_KEY`,
`MINIMAX_API_KEY`, `NVIDIA_API_KEY`, and `OPENROUTER_API_KEY`. `MODEL_API_KEY`
remains available for a single-provider caller, but a fallback setup should use
the named secrets so credentials can never be sent to the wrong provider.

Role workflows receive dedicated `AGENT_FACTORY_STEWARD_*`,
`AGENT_FACTORY_BUILDER_*`, and `AGENT_FACTORY_REVIEWER_*` credentials. They mint
short-lived installation tokens so agent-authored work has a distinct App
identity and never relies on a long-lived personal token. The gate fails closed
unless it finds a current-head approval carrying the factory's machine-readable
review contract.

The alpha is self-hosted. Repositories controlled by one operator may share
that operator's role Apps through centrally managed secrets. Independent
adopters should create their own role Apps and keep their private keys; a
public installation of somebody else's App is not sufficient because caller
workflows must never receive that App owner's private key. A future hosted
service can offer shared managed identities by minting tokens on the server
side. Until then, consumer-owned Apps are the secure public-adoption path.

## Design rule

The factory may define what a current, approved, evidence-backed change means.
It must not encode how a particular product is built, tested, rendered, or
deployed. Those capabilities are discovered from the adopting repository.

Read [Architecture](docs/architecture.md) for the system boundary and
[The repository should brief the agent](docs/the-repository-should-brief-the-agent.md)
for the project thesis.

Trying Agent Factory in another repository? Use the
[adoption review](.github/ISSUE_TEMPLATE/adoption-review.yml) issue form. It
asks for the first failure boundary, separates reusable Factory gaps from
consumer context gaps, and treats an existing issue as the default place for a
continuing concern.
