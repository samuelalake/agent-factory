# Design-ready task packet

Use this packet when a user-facing task needs an explicit design handoff. Keep
it concise and link to authoritative artifacts rather than copying them.
Omit sections that genuinely do not apply; do not fill gaps with guesses.

## Outcome

- **User outcome:** What becomes possible or meaningfully better?
- **Scope:** What bounded slice will be delivered?
- **Mode:** Reproduce, Extend, Design, or Systemize.
- **Source concern:** Link to the intake record, existing issue, or evidence.

## Design authority

- **Product authority:** The current product surface or behavior to preserve.
- **Design-system authority:** Components, tokens, assets, and conventions.
- **Task artifact:** The approved screen, flow, prototype, or source artifact.
- **Conflicts resolved:** Decisions made when authorities disagreed.

## Approved experience

- **Composition:** Structure, hierarchy, grouping, and responsive behavior.
- **Interaction:** Triggers, transitions, completion, interruption, and recovery.
- **States:** Relevant default, empty, loading, error, success, disabled,
  selected, permission, and reduced-motion states.
- **Content:** Approved copy or explicit content latitude.
- **Accessibility and platform behavior:** Requirements that affect the design.

## System use

- **Reuse:** Existing components, tokens, assets, or patterns to use.
- **Extension:** Approved changes to the system, if any.
- **Exact:** Decisions Builder must reproduce without reinterpretation.
- **Adaptable:** Decisions Builder may adjust within named constraints.
- **Excluded:** Nearby work that does not belong in this delivery.

## Delivery contract

- **Known implementation constraints:** Facts already established from the
  codebase or platform.
- **Acceptance:** Observable behavior that constitutes delivery.
- **Evidence:** Required screenshots, recordings, previews, tests, or other
  artifacts, including device or viewport when relevant.
- **Amendment path:** Who decides when implementation exposes a design conflict.

## Approval

- **Status:** Needs design, needs decision, approved for Build, or superseded.
- **Approver:** The configured design authority.
- **Approved artifact/version:** Stable link, identifier, or revision.
- **Open decisions:** None when status is approved for Build.

Approval applies to this packet and its named artifact. It does not authorize
unrelated implementation, publication, deployment, or release actions.
