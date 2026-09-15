# Claude Code Project Rules

Before any repository work, read and follow `docs/23_[C3P_통합수행규칙]_단일계획_프로젝트대화창_및_순차스케줄.md`.

This file is a thin Claude Code adapter. The canonical cross-tool plan and execution rules live only in `docs/23`.

Mandatory rules:

* Use one project session per work item named `[C3P][Wnn][BUILD] 작업명`.
* Implement only a complete `PLAN_LOCKED` task card and stay within its declared files and actions.
* Do not revise the canonical plan, expand scope, merge, discard, deploy, or start another work ID autonomously.
* Return a `BUILT`, `BLOCKED`, or `NEEDS_REVIEW` handoff card with commands, exit codes, changed files, artifacts, and remaining risks.
* Stop writing after handoff until Codex issues the next state transition.

