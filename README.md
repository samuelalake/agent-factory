# Agent Factory

Agent Factory is a reusable, GitHub-native control plane for software agents.
It separates the orchestration loop that every repository needs from the
project adapter that only one repository understands.

The control plane owns:

- event and state contracts for review, triage, build, and merge gating;
- deterministic gate evaluation with one required `agent-factory` status;
- safe review publishing and fail-closed degradation;
- installation and versioned updates of thin caller workflows;
- fixture-driven contract tests.

Consumer repositories own:

- build, test, and evidence commands;
- protected paths and domain-specific verification;
- prompts, context files, and project skills;
- deployment and product policy.

Swami is the first consumer and compatibility fixture. Its Origami parser,
SwiftUI code generator, pixel oracle, and pattern-translation skills remain in
Swami; they are not factory code.

## Status

The repository is private while the extraction boundary and licensing are
settled. The current private alpha implements the configuration and review
artifact contracts, pure gate engine, GitHub review/gate adapters, reusable
caller workflows, and an idempotent installer.

Private cross-owner reusable workflows are not a supported distribution path:
a consumer outside the factory repository owner cannot use these callers while
the factory remains private. Choose public visibility and a license, or package
the runtime through a private registry, before migrating Swami.

## Quick start

```bash
python3 -m pip install -e .
agent-factory init /path/to/consumer --factory-ref main
python3 -m unittest discover -s tests -v
```

The installer creates `.agent-factory/config.json` and thin workflows under
`.github/workflows/`. It refuses to overwrite existing files unless `--force`
is explicit.

The review workflow requires `ANTHROPIC_API_KEY`, `AGENT_FACTORY_APP_ID`, and
`AGENT_FACTORY_APP_PRIVATE_KEY`. It mints a short-lived installation token so
formal reviews have a distinct App identity and never rely on a long-lived PAT.
The gate fails closed unless it finds a current-head approval carrying the
factory's machine-readable review contract.

## Design rule

The factory may know that a reviewer produced a P1. It must not know how to
pixel-compare an Origami patch, compile an iOS target, or deploy a particular
service. Those belong to consumer adapters.
