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

```text
Issue → Steward → Builder → Reviewer → Gate → Steward lands
```

Steward turns intent into executable work. Builder owns implementation and its
evidence. Reviewer evaluates the exact delivered head. A deterministic gate—not
another agent—decides whether Steward may land it. Failed review or integration
returns to the loop; ambiguous or exhausted work returns to Steward.

Read [How work moves](docs/how-work-moves.md) for verification, integration,
delivery evidence, arbitration, and failure behavior.

## Status

Agent Factory is a public alpha. The implementation includes a versioned
configuration contract; executable Steward, Builder, and Reviewer roles;
current-head machine contracts; a pure priority-ordered gate; a deterministic
integration/landing adapter; short-lived GitHub App authentication; reusable
caller workflows; and an idempotent installer.

GitHub's repository-level automatic branch deletion owns post-merge cleanup.
Steward owns the integration decision, not branch housekeeping.

Builder runs a headless agentic harness — a pinned Gemini CLI, a bounded
OpenAI-compatible tool loop, or the subscription-backed Claude Code CLI — and can
fall back to a bounded NVIDIA Kimi tool loop. Model credentials are withheld from
publication steps, GitHub credentials are withheld from model tool environments,
and the fallback refuses to mix with a partially modified primary workspace. This is
still an alpha: dispatch labels should be restricted to trusted maintainers and
the workflows should be evaluated in trusted repositories before hands-off use.

## Quick start

```bash
python3 -m pip install -e .
agent-factory init /path/to/repository --factory-ref <factory-commit-sha>
```

The installer creates `.agent-factory/config.json` and thin Steward, Builder,
Reviewer, and Gate workflows under `.github/workflows/`. It preserves existing
files unless `--force` is explicit. The generated Builder caller uses
`ubuntu-latest`; a consumer that requires a different toolchain changes the
caller's `runner` input while keeping the role contract unchanged.

Next, [provision the consumer and choose its providers](docs/adopting-a-repository.md).
That guide covers GitHub Apps, organization variables and secrets, provider
selection, cost limits, visual evidence, optional authenticated Figma canvas
work, and updating older callers.

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
