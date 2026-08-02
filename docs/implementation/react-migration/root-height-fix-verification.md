# React root-height regression verification

## Changed field

The full-viewport selector in `apps/web/src/styles.css` now covers the React mount point `#root` in addition to the legacy `#app` selector and `.app-shell`.

```text
baseline_sha256: 8A0DD2C6E970E420BA804A9304953BC91ACC84A6237388EBD6075DEEC5F3304C
modified_sha256: 6D67BC9D1CF70EE18923EA3D7A78530D75AA609B362FEC27AB6D6A5D89F02B25
```

## Browser result

Home route at `1280 × 720`:

```text
viewport=1280x720
root=1280x720
app-shell=1280x720
sidebar=268x720
main=1012x720
new-screen=1012x720
document=1280x720
console_warnings=0
console_errors=0
```

Data-modeling route at `1280 × 720`:

```text
modeling-shell=1280x720
topbar=1280x68
modeling-split=1280x652
agent-pane=350x652
document=1280x720
console_warnings=0
console_errors=0
```

Visual evidence:

- `audit-current/react-root-height-fix-home-1280x720.png`
- `audit-current/react-root-height-fix-data-1280x720.png`
- `audit-current/react-root-height-fix-comparison/compare-home-1280x720.png`
- `audit-current/react-root-height-fix-comparison/compare-data-1280x720.png`

## Commands

```powershell
npm run check
npm run build
powershell -NoProfile -ExecutionPolicy Bypass -File tools/verify-react-migration.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tools/rollback-root-height-fix.ps1
```

```text
npm run check
output: ESLint completed with 0 errors
exit_status: 0

npm run build
output: 67 modules transformed; dist/index.html, CSS and JS emitted; built in 781ms
exit_status: 0

tools/verify-react-migration.ps1
output: PASS react_screens=14 legacy_demo=ABSENT build=FOUND assets=FOUND root_viewport=PASS
exit_status: 0

tools/rollback-root-height-fix.ps1
output: ROLLBACK_READY target=E:\Projects\opensource\OpenMathModel\apps\web\src\styles.css mode=preview
exit_status: 0
```
