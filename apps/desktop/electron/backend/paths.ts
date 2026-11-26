import fs from 'node:fs'
import path from 'node:path'

export interface BackendLaunchConfig {
  command: string
  args: string[]
  cwd: string
  mode: 'override-bin' | 'override-python' | 'development' | 'packaged-exe' | 'packaged-python'
}

export interface ResolveBackendPathsOptions {
  isPackaged: boolean
  appRoot: string
  resourcesPath?: string
  platform?: NodeJS.Platform
  env?: Record<string, string | undefined>
  existsSync?: (targetPath: string) => boolean
}

/**
 * Resolve how to launch the Python backend without conflating development and
 * packaged Electron directory layouts. Never constructs shell command strings.
 */
export function resolveBackendLaunchConfig(
  options: ResolveBackendPathsOptions,
): BackendLaunchConfig {
  const platform = options.platform ?? process.platform
  const env = options.env ?? process.env
  const existsSync = options.existsSync ?? fs.existsSync
  const isWindows = platform === 'win32'

  // 1. Explicit packaged sidecar binary override (e.g., testing or custom packaging)
  const overrideBin = env.ROBBIE_BACKEND_BIN?.trim()
  if (overrideBin) {
    return {
      command: overrideBin,
      args: [],
      cwd: path.dirname(overrideBin),
      mode: 'override-bin',
    }
  }

  // 2. Explicit Python interpreter override
  const overridePython = env.ROBBIE_BACKEND_PYTHON?.trim()
  if (overridePython) {
    const backendCwd = options.isPackaged
      ? path.join(options.resourcesPath ?? options.appRoot, 'backend')
      : path.resolve(options.appRoot, '..', '..', 'backend')
    return {
      command: overridePython,
      args: ['-m', 'robbie'],
      cwd: backendCwd,
      mode: 'override-python',
    }
  }

  // 3. Packaged Electron application layout (extraResources under process.resourcesPath)
  if (options.isPackaged) {
    const resourcesRoot = options.resourcesPath ?? options.appRoot
    const packagedBackendDir = path.join(resourcesRoot, 'backend')
    const packagedExe = path.join(
      packagedBackendDir,
      isWindows ? 'robbie-backend.exe' : 'robbie-backend',
    )
    if (existsSync(packagedExe)) {
      return {
        command: packagedExe,
        args: [],
        cwd: packagedBackendDir,
        mode: 'packaged-exe',
      }
    }

    const packagedPython = isWindows
      ? path.join(packagedBackendDir, 'venv', 'Scripts', 'python.exe')
      : path.join(packagedBackendDir, 'venv', 'bin', 'python')
    if (existsSync(packagedPython)) {
      return {
        command: packagedPython,
        args: ['-m', 'robbie'],
        cwd: packagedBackendDir,
        mode: 'packaged-python',
      }
    }

    throw new Error(
      `Packaged Robbie backend executable not found under '${packagedBackendDir}'. ` +
        `Expected '${packagedExe}' or '${packagedPython}'.`,
    )
  }

  // 4. Development monorepo layout: <repo>/apps/desktop -> <repo>/backend/.venv
  const repoRoot = path.resolve(options.appRoot, '..', '..')
  const backendDir = path.join(repoRoot, 'backend')
  const venvPython = isWindows
    ? path.join(backendDir, '.venv', 'Scripts', 'python.exe')
    : path.join(backendDir, '.venv', 'bin', 'python')

  if (!existsSync(venvPython)) {
    throw new Error(
      `Development backend virtual environment not found at '${venvPython}'. ` +
        `Run scripts/install-backend.ps1 first.`,
    )
  }

  return {
    command: venvPython,
    args: ['-m', 'robbie'],
    cwd: backendDir,
    mode: 'development',
  }
}
