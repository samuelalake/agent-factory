# Adopting Agent Factory

This guide starts after `agent-factory init` has created the consumer's config
and thin caller workflows.

## Brief the roles

`project.context_files` names the repository's durable guidance.
`project.skill_dirs` names local skill catalogs; each role selects a bounded
set whose name and description match the current task. The adopting repository
therefore controls both the knowledge and the procedures a role receives.

The [`skills/`](../skills/) directory includes two optional starting points for
adopters: project stewardship that prevents issue-sprawl, and human-facing
writing. They are intentionally small. Domain judgment and project-specific
verification still belong in each adopting repository.

## Provision the consumer

Provisioning and provider selection are separate steps:

1. Install the Steward, Builder, and Reviewer Apps on the consumer repository.
2. Expose their App IDs as organization Actions variables and their private PEM
   keys as organization Actions secrets, restricted to the selected consumer.
3. Expose each model credential the consumer may use, such as
   `CLAUDE_CODE_OAUTH_TOKEN` or `OPENROUTER_API_KEY`, to that same repository.
4. Generate or update the thin callers from a trusted immutable Factory ref.
   Current generated callers forward every supported named provider credential;
   older callers must be regenerated or updated before a newly provisioned
   credential can reach a reusable role workflow.
5. Select the primary and optional fallback provider/model pairs in
   `.agent-factory/config.json`.

A credential being present does not activate its provider. All three conditions
must hold: GitHub exposes the credential to the repository, the caller forwards
the corresponding named secret, and the role config selects that provider as a
primary or fallback. This separation makes it safe to provision an organization
credential before deciding which consumers and roles should spend it.

Role workflows receive dedicated `AGENT_FACTORY_STEWARD_*`,
`AGENT_FACTORY_BUILDER_*`, and `AGENT_FACTORY_REVIEWER_*` credentials. They mint
short-lived installation tokens so agent-authored work has a distinct App
identity and never relies on a long-lived personal token. The gate fails closed
unless it finds a current-head approval carrying the factory's machine-readable
review contract.

For organization-owned consumers, provision the three public App IDs once as
organization Actions variables (`AGENT_FACTORY_STEWARD_APP_ID`,
`AGENT_FACTORY_BUILDER_APP_ID`, and `AGENT_FACTORY_REVIEWER_APP_ID`) and the
three private PEM keys as organization Actions secrets with the corresponding
`_PRIVATE_KEY` names. Restrict both sets to selected consumer repositories.
Generated callers prefer the variables for App IDs while retaining the legacy
same-name secret inputs as a compatibility fallback. This keeps each role's
separate identity and least-privilege App installation without asking every
consumer repository to duplicate six values. Provider credentials such as
`CLAUDE_CODE_OAUTH_TOKEN` can use the same selected-repository organization
secret pattern. Private repositories on GitHub Free may still require
repository-level secrets because GitHub does not expose organization Actions
secrets to them.

The self-hosted Apps need these repository permissions:

- **Steward:** Contents write, Issues write, and Pull requests write. Contents
  write is required for the merge itself; Pull requests write alone is not.
- **Builder:** Contents write, Issues write, and Pull requests write.
- **Reviewer:** Contents read and Pull requests write.

Reusable workflows request their exact subset while minting each token. Steward
first receives only its communication permissions, then separately proves its
landing authority. A mismatch therefore fails at authentication instead of
after a long build or verification wait, while Steward can still replace stale
status prose with an actionable failure record under its own identity.

The alpha is self-hosted. Repositories controlled by one operator may share
that operator's role Apps through centrally managed secrets. Independent
adopters should create their own role Apps and keep their private keys; a
public installation of somebody else's App is not sufficient because caller
workflows must never receive that App owner's private key. A future hosted
service can offer shared managed identities by minting tokens on the server
side. Until then, consumer-owned Apps are the secure public-adoption path.

## Choose providers

The runtime is model-agnostic: a role chooses a primary harness/provider and an
optional fallback pair through versioned configuration. Provider choice does
not change the review or gate contract. Builder supports Gemini CLI, bounded
OpenAI-compatible loops for MiniMax, NVIDIA, and OpenRouter, and a `claude-code`
harness that runs the pinned Claude Code CLI headless in edit mode with a Pro or
Max subscription token. Reviewer supports the provider-neutral text and image
adapters plus that same subscription-backed `claude-code` harness. For visual
review, the CLI receives authenticated evidence as local files and may use only
its read-only image tool. Steward supports both the subscription-backed
`claude-code` route and the API-backed adapters for Anthropic, Gemini, MiniMax,
NVIDIA, and OpenRouter. The generated configuration starts with Gemini and falls
back to NVIDIA Kimi; both are quota-limited services, so the delivery record
names the provider and model that actually served the run instead of implying
that a free tier is unlimited.

Caller workflows pass provider-specific secrets such as `GEMINI_API_KEY`,
`MINIMAX_API_KEY`, `NVIDIA_API_KEY`, and `OPENROUTER_API_KEY`. `MODEL_API_KEY`
remains available for a single-provider caller, but a fallback setup should use
the named secrets so credentials can never be sent to the wrong provider.
The `claude-code` provider — available to Steward, Builder, and Reviewer — instead
requires `CLAUDE_CODE_OAUTH_TOKEN`, generated with `claude setup-token`; it never
receives `ANTHROPIC_API_KEY`, because that variable would override subscription
billing in Claude Code print mode. Configure its model with a Claude Code model
name or alias such as `opus`. Steward intake, Reviewer work, and visual evidence
arbitration can run on the subscription route. Visual use is opt-in: enable the
corresponding visual-capability flag only after the exact route passes its
provider smoke. The Builder route additionally sets
`builder.harness: "claude-code"`; because it runs on a flat subscription rather
than metered tokens, its delivery record reports the model cost as
`subscription` and the fallback pair stays on a metered OpenAI-compatible
provider.

## Bound cost and retries

Builder configuration can set `max_model_requests`, `max_output_tokens`,
`max_model_cost_usd`, `input_cost_per_million`, and
`output_cost_per_million`. The cost ceiling is calculated from provider-reported
token usage. OpenRouter Builder requests also send those rates as a provider
`max_price`, preventing routing to an endpoint that costs more than Factory's
estimate; both rates must therefore be positive and conservative for the chosen
model. For OpenRouter, Factory enforces the provider-reported `usage.cost` after
every response and shares one cost budget across primary and fallback attempts,
so cache, fallback, and other billed usage cannot escape token-only accounting.
Keep a provider-side account or key budget as the authoritative hard stop because
a response is billed before its usage can be evaluated.

`max_revision_attempts` bounds the Steward-managed Builder → Reviewer repair
loop. On each clean revision, Builder receives the current-head review findings;
after the limit, Steward retains the blocker instead of creating an infinite loop.

Builder revisions also receive bounded failed-job diagnostics for the exact PR
head, collected before base synchronization. The collector keeps only the latest
run per workflow in a bounded scan, excludes other heads and repositories, and
labels unavailable logs explicitly. Logs are untrusted diagnostic data, not
instructions or proof that checks passed.

When updating an existing consumer, add `actions: read` to its Builder caller's
`permissions` alongside `contents: read`. The reusable workflow supplies its
short-lived `github.token` as `AGENT_FACTORY_CI_READ_TOKEN` to the harness only;
this credential is excluded from model subprocess environments. The Builder App
continues to own publication and does not need additional Actions permissions.

## Configure visual evidence

For visual consumers, `visual_revision_context: true` gives an
OpenAI-compatible or Claude Code Builder the exact rejected-head screenshots.
Factory fetches those images with Builder App authentication, validates their
repository, head-keyed path, format, per-image size, and aggregate size, then
sends bounded evidence to the model. Enable this only for models whose exact
provider route passes the reusable provider smoke with `visual_input: true`; the
default remains text-only. Fallback routes fail closed during a visual revision
unless `fallback_visual_revision_context: true` is separately configured after
the fallback's exact route passes the same smoke.

Set `review.delivery_wait_seconds` to bound how long Reviewer waits for trusted
consumer evidence. Keep it at least as long as the consumer's slowest required
verification job. Pending, missing, or previous-head deliveries wait within that
bound; a superseded PR head stops the wait immediately. Stale evidence is never
accepted, including when the wait expires.

Set `review.visual_evidence: true` only when the configured Reviewer model accepts
image input. Configure `review.fallback_visual_evidence` independently for the
fallback route. When exact-head Builder evidence reports a deterministic failure,
Reviewer keeps that P1 blocker and uses authenticated evidence images to provide
specific visual corrections. Pending or missing evidence still fails closed
without calling a model.

The `claude-code` Reviewer can inspect authenticated screenshots through the
CLI's read-only image tool. It receives local evidence files rather than inline
API image blocks and cannot edit the checkout in this role.

## Enable authenticated Figma canvas work

The dedicated Claude Code Figma Writer can use Figma's remote MCP server to
create or edit native canvas content after Builder prepares the repository
candidate. This is opt-in because it grants the phase access as a Figma user.
Builder itself receives no Figma token or MCP tools.

1. Add an explicit Figma phase configuration. `lease_key` is a non-secret name
   for the OAuth identity lane; every consumer sharing the same credential set
   must use the same centrally enforced lane or separate credentials.

   ```json
   "figma": {
     "marker": "<!-- figma-writer:agent-factory -->",
     "enabled": true,
     "lease_key": "figma-oauth/samuel-primary",
     "model": "claude-opus-4-8",
     "timeout_seconds": 1800
   }
   ```

   The older `builder.figma_mcp: true` setting remains a temporary compatibility
   alias and receives the conservative `legacy-shared-oauth` lane. New consumers
   should use the explicit phase configuration.
2. From a trusted Agent Factory checkout, run:

   ```bash
   PYTHONPATH=src python3 -m agent_factory.figma_mcp authorize --repo OWNER/REPOSITORY
   ```

3. Approve the Figma MCP connection in the browser window. The command registers
   the supported Claude Code client and uses PKCE, then writes the resulting
   `FIGMA_MCP_ACCESS_TOKEN` directly to GitHub Actions secrets. The command does
   not print any credential value. Re-run it only when Figma expires or revokes
   the grant, the secret is removed, or you intentionally change identities.
4. Regenerate an older Builder caller or manually forward the named credentials
   to the reusable workflow.

After Builder opens or updates a draft pull request, the Figma Writer job waits
for its identity lease. Only then does Factory build a private MCP config and
expose the access grant to Claude Code for that isolated job. The token is not
forwarded to Builder or Reviewer, and the hosted runner is discarded after the
Writer phase. The pull request becomes ready only after the Writer succeeds.
A missing or expired grant, an invalid record, or a missing exact-head
authenticated result leaves the pull request in draft and fails closed.

This connection is distinct from a Figma personal access token or a normal REST
OAuth application. Those credentials support the REST API and related export or
metadata workflows; they do not supply the MCP server's `mcp:connect` grant or
an authenticated native canvas-write session.

Consumers can set `review.visual_evidence_paths` to repository-relative globs such
as `app/**` and `**/*.origami`. A matching change without a canonical Builder
delivery is blocked deterministically, even when the model would approve it. A
non-matching control-plane or documentation change is reviewed from its diff,
checks, and linked evidence without manufacturing unrelated screenshots. Builder
marks that visual delivery as not applicable and binds the decision to the exact
commit, so Reviewer does not wait for an evidence job that correctly did not run.
Reviewer findings must describe concrete defects in the supplied change. A
consumer that pins a reusable workflow is not expected to duplicate that
workflow's enforcement logic locally; dependency-pin review instead evaluates
the consumer wiring and the supplied upstream evidence.

## Publish media evidence

Consumer evidence publishers that attach media must provide GitHub CLI 2.99 or
newer plus `AGENT_FACTORY_MEDIA_UPLOAD_TOKEN`. Prefer a fine-grained personal
access token limited to the consumer repository with Issues and Pull requests
read/write; GitHub CLI uses it only for the disposable staging comment and media
bytes. The Builder App token remains `GH_TOKEN`; it performs the final
pull-request body patch and upserts the authenticated provenance comment.
Configure `builder.app_login` to that App's bot login (the scaffold defaults to
`agent-factory-builder[bot]`). GitHub-hosted runners can install or pin a current
CLI release before calling `agent-factory publish-delivery`.

Visual evidence extraction includes every declared image in delivery order, up
to the publisher's 50-attachment limit, subject to the existing 4 MB per-image
and 12 MB aggregate fetch bounds. Oversized sets fail closed; they are never
silently reduced to the first few images. Consumers should publish a focused,
clearly labeled comparison of the required states (including both platforms
where relevant) instead of relying on image order to survive truncation.
