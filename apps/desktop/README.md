# Robbie Desktop (`apps/desktop`)

Electron + React + TypeScript desktop shell for Robbie (Windows x64).

## Scripts

- `npm run dev` — Start Vite + Electron in development mode.
- `npm run typecheck` — Run TypeScript type checking (`tsc --noEmit`).
- `npm run lint` — Run ESLint with zero warnings allowed.
- `npm run build:app` — Type check and build the renderer (`dist/`) and Electron (`dist-electron/`) bundles.
- `npm run build` — Build bundles and package a Windows NSIS installer via `electron-builder`.

See `docs/DEVELOPMENT.md` at the repository root for full environment setup instructions.
