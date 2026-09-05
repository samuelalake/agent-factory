# Reference skills

Agent Factory ships small role-independent skills that adopting repositories
may use or adapt:

- `project-steward` keeps feedback, work state, and the human-readable project
  story coherent before tasks reach Triage or Build.
- `human-writing` guides agent-authored communication without prescribing a
  product voice.

These are defaults, not hidden global prompts. A repository can replace them,
add domain skills, or omit them. Factory roles discover the adopting
repository's skills and treat local guidance as authoritative for that project.
