# Project structure verification

> **Historical evidence snapshot (2026-08-02), not the current structure guide.** Paths, prototype files, verifier inputs and counts below are intentionally preserved as observed on that date. Use [`PROJECT_STRUCTURE.md`](../../PROJECT_STRUCTURE.md) and the [documentation index](../README.md) for current facts.

This record is populated after running the repository-local verifier and rollback preview.

## Baseline

The static prototype and its existing QA baseline are preserved. Expected hashes are stored in `project-structure-baseline.sha256` and checked by `tools/verify-project-structure.ps1`.

## Commands

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/verify-project-structure.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tools/rollback-project-structure.ps1
```

## Expected behavior

- Baseline: the existing `demo/` implementation retains the recorded SHA-256 hashes.
- Modified: all product boundaries and required documentation exist; large dataset partitions are ignored.
- Rollback preview: reports a ready manifest without changing the workspace.

## Verified result (2026-08-02)

Baseline and modified structure check:

```text
command: powershell -NoProfile -ExecutionPolicy Bypass -File tools/verify-project-structure.ps1
input: required path list + .gitignore + four preserved prototype files
output: PASS required=28 preserved=1 dataset_ignore=PASS
exit_status: 0
```

The single preserved baseline is the SHA-256-verified static-demo source archive created before the React migration.

Rollback readiness check:

```text
command: powershell -NoProfile -ExecutionPolicy Bypass -File tools/rollback-project-structure.ps1
input: docs/implementation/project-structure-files.txt
output: ROLLBACK_READY files=43 mode=preview
exit_status: 0
```
