# React migration verification

> **Historical evidence snapshot.** Commands, audit paths, hashes and page counts below describe the migration worktree at verification time. Use the [Web UI baseline](../../development/web-ui-baseline-and-api-integration.md) for current page rules.

## Baseline

```text
artifact: audit-current/migration-backups/demo-source-before-react.zip
sha256: F93FD810E2B89903E42C915788F48D40A1B7C2B672EDB42F3D31CB8B3420AB01
archive_entries: 29
```

Static QA evidence was reclassified under `audit-current/legacy-static-demo/`; the top-level `demo/` directory was removed after the React build and browser gate passed.

## Modified build

```text
command: npm run check
working_directory: apps/web
output: ESLint completed with 0 errors
exit_status: 0

command: npm run build
working_directory: apps/web
output: 67 modules transformed; dist/index.html and bundled CSS/JS emitted; built in 882ms
exit_status: 0
```

## Structure verification

```text
command: powershell -NoProfile -ExecutionPolicy Bypass -File tools/verify-react-migration.ps1
input: React source, 14 screen declarations, production build, public assets, deleted legacy path
output: PASS react_screens=14 legacy_demo=ABSENT build=FOUND assets=FOUND
exit_status: 0
```

## Browser verification

```text
routes: 14/14 rendered with the expected screen id, title and React root
primary_flow: home composer -> /task/running
data_interaction: details drawer open=true, aria-hidden=false
assets: all rendered images complete with naturalWidth > 0
console: 0 warnings, 0 errors after the final asset/editor fixes
viewport: 1280x720
```

Visual comparison evidence is stored in `audit-current/react-migration-comparison/compare-current-*-1280x720.png`. Final result is recorded in the root `design-qa.md`.

## Rollback readiness

```text
command: powershell -NoProfile -ExecutionPolicy Bypass -File tools/rollback-react-migration.ps1
input: audit-current/migration-backups/demo-source-before-react.zip
output: ROLLBACK_READY archive_entries=29 target=E:\Projects\opensource\OpenMathModel\demo mode=preview
exit_status: 0
```
