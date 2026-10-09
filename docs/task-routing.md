# Validated task branches

Opt in with `validated_routing: true` on the reusable Steward, Builder, Reviewer,
and gate workflows. Existing callers retain their legacy checkout/default behavior.
The existing `builder.base_branch` supplies the default and
`steward.trusted_operator_logins` supplies operator identities. The only new policy
section is:

```json
"routing": {
  "repository": "owner/repository",
  "allowed_base_branches": ["main", "release/demo"]
}
```

The default must be in the explicit allowlist. Any short branch name can be admitted
through a reviewed controller configuration change; globs, tags, PR refs, raw SHA
selectors and revision expressions are not accepted. No cross-repository targets.

Pass `base_ref` on a manual Steward or Builder dispatch to select a branch. With
an empty input, an authenticated existing task route takes precedence, followed by
`builder.base_branch` for a new task. A new selection requires both a configured
operator login and current GitHub write/maintain/admin permission. The named branch
must exist. Reuse also requires a configured operator with current write authority,
or a matching original/triggering configured App actor on an issue event. Preflight uses the read-only workflow token and runs before model/App
credentials reach the execution job.

The controller is the consumer's default-branch checkout; its SHA is distinct from
the selected branch's `source_sha`. Caller workflow and Factory code stay pinned
independently. Policy, providers and Figma enablement come from the controller,
not target code. Keep the caller on the trusted default branch; never dispatch a
secret-bearing caller from arbitrary target code. Reviewer/gate callers on an
admitted base must also use the same reviewed Factory pin and validated routing.

Steward writes an append-only route receipt through its App before adding the Builder label.
Receipt validation rereads its GraphQL node, verifies Bot identity, repository/issue,
exact body and database ID, and requires `lastEditedAt` to be null. The original
REST author alone is insufficient because repository writers can edit others' comments.
Builder retains the same repository, issue, base, source SHA and controller SHA in
its PR and issue receipt. Checkout, merge, diff and candidate validation all use the
source SHA. A routed PR opens as a draft. Builder also records its candidate SHA;
a resumed Builder only executes that authenticated exact head, fetching by SHA
and publishing with an explicit expected-head lease. A user-edited or seeded PR
without that Builder receipt is refused, rather than run with model credentials.
An authorized base branch is executable input: only admit branches whose code and
runner configuration the repository operators trust.

Reviewer and gate require the same authenticated candidate receipt. The gate accepts a formal review only from the configured Reviewer App, with its actual GitHub commit ID, protocol head SHA and current candidate SHA all equal. Reviewer reads target changes as data from a trusted controller checkout. It
validates receipt identity and source ancestry before review and again before its
normal review publication. Publisher can pass `--route-config` to revalidate the
route before upload, before writing the PR body, and after publication. Candidate
head SHA and base source SHA are different facts; both must remain bound. Removing
a route from a PR cannot opt it out when its issue has an authenticated route.

A branch rewrite/deletion, retargeted PR, mismatched receipt, changed untrusted head
or controller revision mismatch fails closed. Controller updates during a task
require operator reconciliation; do not erase receipts to bypass the check or
silently float the task to a newer source. Existing routed PRs cannot be retargeted
by changing only their issue receipt: complete/retire the task and create a new
issue for a different route. Routed branch prefixes without receipts fail closed. A reviewed optional
`routing.legacy_publication_heads` map can admit specific legacy PR numbers at
specific full SHAs for review/publication only. Builder never uses this exception.
Ordinary non-routed branches retain legacy behavior.

Example consumer caller input:

```yaml
with:
  issue: ${{ inputs.issue }}
  factory_ref: <reviewed-full-factory-commit>
  validated_routing: true
  base_ref: ${{ inputs.base_ref }}
```

After activation and explicit operator approval, selecting a branch would use:

```sh
gh workflow run agent-steward.yml --repo owner/repository --ref main \
  -f issue=123 -f base_ref=release/demo
```

Do not run this example merely to validate a draft PR: it starts autonomous work.
The workflow dispatcher needs permission to read issue comments and branch refs.
No new secrets or App permissions are provisioned by this change.

Failure containment is the small opt-in flag from Factory PR107, reconciled onto
current main without depending on that draft's head. `suppress_failure_escalation`
defaults to false. Model provider/fallback behavior is unchanged.
