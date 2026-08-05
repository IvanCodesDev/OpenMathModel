# Agent execution-plane verification

This record covers the first batch of the agent execution plane: `agents/core`
(state machine, events, ports, replay), `agents/tools` (workspace isolation,
tool registry/invoker, Python sandbox), `agents/prompts` + `agents/skills`
(prompt convention, structured-output LLM nodes), `services/worker` (durable
JSONL event log, file queue, run leases, idempotent runtime) and
`agents/evals` (golden end-to-end trajectory).

## Scope and boundaries

- LLM calls are stubbed (`StubLlmPort`); real provider adapters arrive with
  key management in a later batch. Everything else — engine, sandbox
  subprocess execution, queue, leases, artifact hashing — is real.
- Event/TaskRun field names are internal dataclasses; `packages/contracts`
  remains the cross-language source of truth and an adapter will align them
  once its generated Python models land (coordination point with the
  control-plane workstream).
- The JSONL event store and file queue are the MVP storage backends; the
  PostgreSQL event table and Redis queue replace them behind the same ports
  in the infra batch.
- Runtime products (event logs, workspaces, leases, queues) live under the
  gitignored `runs/` tree; `pytest` uses per-test temp directories.
- Package layout follows ADR-0003: src layout (`<member>/src/omm_*`),
  distribution names `omm-agent-core` / `omm-agent-tools` / `omm-agent-skills`
  / `omm-agent-evals` / `omm-worker`, all already registered as members of the
  root uv workspace. Until `uv sync` lands (uv not installed on this machine),
  the verifier imports members via `PYTHONPATH` pointing at the src dirs.

## Commands

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/verify-agent-runtime.ps1
```

The script bootstraps `agents/.venv` (Python 3.12 + pytest) when missing, then
runs each package's suite from its own directory with a shared `PYTHONPATH`.

## Verified result (2026-08-04)

```text
command: powershell -NoProfile -ExecutionPolicy Bypass -File tools/verify-agent-runtime.ps1
input: five package test suites (agents/core, agents/tools, agents/skills, services/worker, agents/evals), src layout per ADR-0003
output:
  PASS agents/core :: 30 passed in 0.03s
  PASS agents/tools :: 21 passed in 2.06s
  PASS agents/skills :: 26 passed in 0.06s
  PASS services/worker :: 25 passed in 1.38s
  PASS agents/evals :: 5 passed in 0.95s
  PASS suites=5 runtime=agent-execution-plane
exit_status: 0
environment: Windows, Python 3.12.7 (agents/.venv), pytest 9.1.1
```

## What the suites prove

- **State machine** (`agents/core`): legal-transition matrix, no stage
  skipping, review gate semantics, retry re-entry, terminal freezing.
- **Event sourcing**: engine mutates snapshots only through the same reducer
  replay uses; every scenario asserts `replay(events) == live snapshot`;
  sequence gaps/duplicates are rejected; sinks deduplicate redeliveries.
- **Tool safety** (`agents/tools`): path traversal and absolute paths
  rejected after resolution, workspace quota enforced, tool allowlist
  enforced, handler crashes/timeouts contained, subprocess sandbox kills on
  timeout, scrubs the environment and caps output.
- **Worker semantics** (`services/worker`): duplicate job deliveries are
  harmless (idempotent), a crash mid-step is healed into an explicit failed
  attempt and re-run (attempt+1), abandoned claims are requeued after TTL,
  poison jobs park in `dead/`, run leases are exclusive, stealable only
  after expiry and safe against stale releases.
- **End to end** (`agents/evals`): one full TaskRun goes
  `CREATED → … → NEEDS_REVIEW → (approve) → … → COMPLETED` through the real
  queue and lease, executes real Python (least squares) in the sandbox, and
  produces `metrics.json` + `report.md` artifacts whose SHA-256 checksums
  match the event log; the full 25-event trajectory is asserted exactly.

## Known NOT RUN

- Real LLM provider round-trips (no key material in this batch).
- Cross-process concurrency races (single-process atomic-rename/O_EXCL
  semantics are OS-level; multi-process stress lands with the infra batch).
- Integration with `services/api` SSE and the PostgreSQL event table
  (explicit next-batch coordination point with the control-plane workstream).
- `uv sync` / `uv lock`, root `mypy --strict` and `ruff` gates from
  ADR-0002/0003 (uv, mypy and ruff are not installed on this machine yet;
  pytest suites above are the executed evidence). ADR-0003's target of
  `omm-agent-core` depending on `omm-contracts` + pydantic is deferred to the
  contracts-alignment batch — the core currently has zero runtime
  dependencies by design.
