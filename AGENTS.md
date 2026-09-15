# Project Agent Rules

Before any repository planning, implementation, review, or verification, read and follow `docs/23_[C3P_통합수행규칙]_단일계획_프로젝트대화창_및_순차스케줄.md`.

This file is a thin Codex adapter. The canonical cross-tool plan and execution rules live only in `docs/23`.

Mandatory rules:

* Use a project-scoped task named `[C3P][Wnn][PLAN] 작업명`.
* Do not combine multiple work IDs in one task.
* Codex owns planning, task-card issuance, synthesis, and closure; it must not modify files owned by an active BUILD or VERIFY stage.
* Do not start implementation before `PLAN_LOCKED` or advance to the next work ID before the current one is `CLOSED`.
* Treat missing evidence and unresponsive tools as `UNKNOWN`, never as agreement or success.

