# Antigravity Project Rules

Before any repository work, read and follow `docs/23_[C3P_통합수행규칙]_단일계획_프로젝트대화창_및_순차스케줄.md`.

This file is a thin Antigravity adapter. The canonical cross-tool plan and execution rules live only in `docs/23`.

Mandatory rules:

* Use one project conversation per work item named `[C3P][Wnn][VERIFY] 작업명`.
* Begin verification only after receiving a `BUILT` handoff for the same work ID.
* Verification is read-only with respect to implementation code. Report defects as evidence-backed cards instead of fixing them in the VERIFY conversation.
* Do not revise the canonical plan, expand scope, merge, discard, deploy, or start another work ID autonomously.
* Return a `VERIFIED`, `BLOCKED`, or `NEEDS_REVIEW` handoff card and wait for Codex to close or reopen the work item.

