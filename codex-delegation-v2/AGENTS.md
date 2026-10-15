# Codex routing — Plus subscription / quota-conscious

Goal: minimize account usage and repeated failed work while preserving verified correctness. This is a policy, NOT a guaranteed programmable routing algorithm.

## Dispatch
- L0: First use shell tools (`rg`, `git status`, `git diff`, tests) to answer exact searches and detect scope; avoid reading entire repo.
- L1: Handle tiny, deterministic, well-localized edits in the main session. For nontrivial reconnaissance, delegate to `explorer` only if isolation saves parent context.
- L2: For scoped multi-step code changes, use `implementer` when a separate workspace/context is materially beneficial.
- L3: Reserve `reviewer` for complex changes, security, data loss, critical formulas, cross-module regressions, or persistent unexplained failures.
- Do not spawn agents reflexively; each subagent consumes quota. Keep at most two concurrent, and parallelize only independent subtasks.
- The main session is coordinator, not a mandatory expensive planning phase. Avoid elaborate plans for simple tasks.

## Context budget (soft guidance, not enforced token limits)
- Prefer file path + symbol + relevant lines to entire files. Do not paste whole logs or large JSON when an excerpt suffices.
- Explore <= 5 most likely source files first; enlarge scope if facts demand it.
- Agent handoff: objective, exact relevant paths, constraints, success checks, short prior findings; aim for <= 15 lines. Do not repeat unchanged context.
- Default output: concise changes + tests + risks, no long narrative. Do not hide unresolved issues.
- Never silently truncate code required to make a correct decision.

## Failure and escalation
- After first failed attempt, inspect the actual error and revise hypothesis; do not blindly retry.
- After second genuinely distinct failed attempt or ambiguous systemic flaw, consider reviewer/high effort with concise evidence.
- If high-risk from start, review proactively. Validate using tests/tools wherever possible.
- Never claim a subagent or test ran unless it actually ran. If configured model is unavailable, report it and ask to select an available model.
- Respect user changes, permissions, and scope. Do not commit/push/install globally without user request.

## End-of-task status
- Report: files changed, check commands/results, remaining risk, and whether any subagents were used.
- If asked to measure efficiency, ask user to record approximate usage/limit changes, not fabricated per-agent token measurements.
