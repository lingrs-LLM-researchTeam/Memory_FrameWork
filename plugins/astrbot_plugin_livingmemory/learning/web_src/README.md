# Self Learning Dashboard

This directory contains the Solid.js, TypeScript, SCSS, Vite and ECharts sources for the optional learning module's standalone WebUI. The module is part of LivingMemory Framework and is not installed as a separate plugin.

Use Node.js 22 and pnpm 10. Run in this directory:

```sh
pnpm install --frozen-lockfile
pnpm dev
pnpm typecheck
pnpm test
pnpm build
```

The development server runs at `http://localhost:3000` and proxies `/api` and `/static` to `http://127.0.0.1:7833`. Set `VITE_DASHBOARD_PROXY` to use another backend. Enable and configure the standalone WebUI before using live data.

Production output goes directly to `../web_res/static/dashboard/`, served under `/static/dashboard/`. Commit these runtime assets so installations without Node.js work. The AstrBot embedded Pages are separate. Missing production assets produce a 503 build prompt.

Keep existing `#/page-name` routes, browser appearance preferences and backend API contracts. Pause background refresh while editing, confirm destructive UI actions, and release ECharts/ResizeObserver resources when components unmount.

Framework setup, module behavior and development guidance are consolidated in the [root README](../../../../README.md#development).
