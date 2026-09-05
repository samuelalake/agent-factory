# The repository should brief the agent

Most agent configuration starts from the role: write a prompt that tells a
reviewer how to review, a triage bot how to label, or a builder which files it
may create. This works for a demonstration. It becomes brittle when the work
changes.

Software work is rarely one operation repeated forever. The same repository
may need a parser repaired, a migration planned, documentation reconciled, a
visual regression investigated, or an unfamiliar test harness restored. A
useful agent must understand the task in the context of the codebase, discover
the relevant skills, use available tools, and produce evidence appropriate to
the change.

That leads to a different design principle:

> Configure an agent's mission, capabilities, permissions, and completion
> contract. Let the repository teach it how the current task should be done.

## Repository-native context

Project knowledge already exists in code, tests, decision records, issue
history, contribution guidance, and local skills. Agent Factory treats those
as an operating contract rather than a collection of documents pasted into one
large prompt.

An agent begins with lightweight role context. It then discovers what is
relevant:

- repository guidance establishes durable conventions;
- decisions explain load-bearing architectural choices;
- skills provide procedures for specialized work;
- commands and adapters expose build, test, and evidence capabilities;
- issues and pull requests hold the current state of the work.

The important property is not that every agent receives all context. It is
that every agent can find the right context before acting and can identify the
evidence its claim requires.

## Apps make agency visible

GitHub Actions is good infrastructure identity. It should continue to own
deterministic jobs such as tests, publishing, and status computation. An agent
performing judgment or authoring work benefits from a distinct identity.

Agent Factory separates those identities by role:

- **Triage** interprets new work, finds related history, and makes the queue
  actionable.
- **Builder** inspects the repository, selects relevant skills, changes code,
  runs verification, and opens a pull request with evidence.
- **Reviewer** evaluates the current head against repository decisions and
  submitted evidence, then approves or leaves testable findings.

Separate GitHub Apps make the activity legible to people and constrain each
role to least privilege. A reviewer does not need branch-write access. A
builder does not need permission to approve its own change. Triage should not
be able to alter source code.

The visual distinction is not decoration. It communicates who is acting, what
authority the action carries, and where responsibility changes hands.

These roles form a staged loop: intent enters Triage; Builder inspects and
implements; deterministic Verify jobs produce evidence; Reviewer judges the
current head; Gate computes policy; landing and learning write durable context
back to the repository. Build, Verify, and Review can repeat until the evidence
and current-head verdict agree.

## Models are adapters

No role should be synonymous with one model vendor. A provider is an execution
adapter selected for capability, cost, latency, and availability. The role's
mission, repository context, tools, permissions, output contract, and gate
semantics remain stable when the model changes.

That makes model choice explicit rather than accidental. A cheaper or free-tier
model can serve routine work; a stronger model can handle riskier changes. A
future fallback policy can change adapters between attempts without changing
the role contract. The artifact should record which provider served the run,
while policy evaluates the normalized result rather than granting a vendor
special authority.

## Models propose; policy decides

Agent output is untrusted, even when it is useful. A model can summarize a
change and identify findings, but it should not define whether its own output
is fresh or sufficient.

The factory therefore separates human-readable prose from machine-readable
state. Review artifacts include a versioned contract with the reviewed commit,
verdict, and normalized findings. A deterministic gate independently checks:

1. whether the branch is mergeable;
2. whether required verification passed;
3. whether the review belongs to the current head;
4. whether the reviewer approved;
5. whether blocking findings remain;
6. whether nonblocking findings were addressed or explicitly tracked.

The first applicable rule wins. Failure paths leave visible state instead of
silently becoming approval.

## Beyond the reviewer bot

Code review products increasingly index an entire repository, accept custom
rules, and learn from developer feedback. That is valuable, but review is only
one transition in the work.

Agent Factory's larger proposition is a repository-owned loop. The same
context and evidence language can guide issue intake, implementation, review,
and merge readiness. The implementation agent can consume a review finding;
the reviewer can evaluate builder evidence; the gate can validate both without
depending on either agent's prose.

This also complements orchestration systems that turn project-board items into
isolated coding runs. Orchestration answers when and where an agent runs. The
factory focuses on how role identities, repository knowledge, skills, evidence,
and deterministic policy compose around that run.

## What the alpha still has to prove

A reusable control plane cannot be validated by its source repository alone.
The alpha needs multiple adopting repositories with different languages,
verification commands, and product constraints.

The meaningful measures are not how many comments an agent writes or whether a
review UI displays a perfect score. They are:

- valid defects found and regressions prevented;
- false positives and successful evidence-backed rebuttals;
- findings that lead to completed changes;
- time and model cost per accepted change;
- correct selection of repository context and skills;
- recovery from stale reviews, missing credentials, provider failures, and
  interrupted runs;
- safe upgrades without copied orchestration drifting between repositories.

The goal is not to configure one bot to do one thing. It is to make a codebase
legible and operable to a changing set of capable agents while keeping their
authority narrow and their claims verifiable.
