---
name: human-writing
description: Write concise, specific project communication for people. Use for issue and pull request prose, status updates, decision records, review summaries, and agent-authored documentation; do not use to rewrite untouched user prose or technical identifiers.
---

# Human writing

Write for a person who needs to understand what changed and decide what to do.

- Lead with the outcome, decision, or blocker.
- Name concrete files, behavior, evidence, versions, and owners when useful.
- Separate observed fact, inference, and proposal.
- Describe user-visible behavior before implementation detail.
- Say what is unresolved and who can resolve it.
- Use short sentences and natural rhythm. Prefer plain verbs.
- Remove generic praise, promotional language, decorative headings, repeated
  summaries, and filler conclusions.
- Avoid formulaic contrasts, forced lists of three, excessive bold text, and
  abstract metaphors that hide the mechanism.
- Do not claim completion from a commit, merged pull request, or green test when
  the requested outcome still lacks evidence.

Preserve the author's voice and existing terminology. Edit only prose in scope;
do not rename code, labels, API fields, or quoted user language to satisfy a
style preference.

Before publishing, ask: what can the reader now know or decide that they could
not before? Remove sentences that do not help answer that question.
