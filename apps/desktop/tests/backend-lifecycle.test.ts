import assert from 'node:assert/strict'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { BackendSidecarManager } from '../electron/backend/manager.ts'
import { resolveBackendLaunchConfig } from '../electron/backend/paths.ts'
import { LoopbackWebSocketClient } from '../electron/backend/wsClient.ts'
import type { BackendEvent, BackendStatusSnapshot } from '../shared/backend.ts'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const DESKTOP_ROOT = path.resolve(__dirname, '..')

function isProcessAlive(pid: number): boolean {
  try {
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}

test('resolveBackendLaunchConfig distinguishes development vs packaged paths', () => {
  const devConfig = resolveBackendLaunchConfig({
    isPackaged: false,
    appRoot: 'C:\\repo\\apps\\desktop',
    platform: 'win32',
    env: {},
    existsSync: (p) => p === 'C:\\repo\\backend\\.venv\\Scripts\\python.exe',
  })
  assert.equal(devConfig.mode, 'development')
  assert.equal(devConfig.command, 'C:\\repo\\backend\\.venv\\Scripts\\python.exe')
  assert.deepEqual(devConfig.args, ['-m', 'robbie'])
  assert.equal(devConfig.cwd, 'C:\\repo\\backend')

  const packagedExeConfig = resolveBackendLaunchConfig({
    isPackaged: true,
    appRoot: 'C:\\Program Files\\Robbie\\resources\\app.asar',
    resourcesPath: 'C:\\Program Files\\Robbie\\resources',
    platform: 'win32',
    env: {},
    existsSync: (p) => p === 'C:\\Program Files\\Robbie\\resources\\backend\\robbie-backend.exe',
  })
  assert.equal(packagedExeConfig.mode, 'packaged-exe')
  assert.equal(
    packagedExeConfig.command,
    'C:\\Program Files\\Robbie\\resources\\backend\\robbie-backend.exe',
  )
  assert.deepEqual(packagedExeConfig.args, [])

  const packagedPythonConfig = resolveBackendLaunchConfig({
    isPackaged: true,
    appRoot: 'C:\\Program Files\\Robbie\\resources\\app.asar',
    resourcesPath: 'C:\\Program Files\\Robbie\\resources',
    platform: 'win32',
    env: {},
    existsSync: (p) =>
      p === 'C:\\Program Files\\Robbie\\resources\\backend\\venv\\Scripts\\python.exe',
  })
  assert.equal(packagedPythonConfig.mode, 'packaged-python')
  assert.deepEqual(packagedPythonConfig.args, ['-m', 'robbie'])

  assert.throws(
    () =>
      resolveBackendLaunchConfig({
        isPackaged: true,
        appRoot: 'C:\\Program Files\\Robbie\\resources\\app.asar',
        resourcesPath: 'C:\\Program Files\\Robbie\\resources',
        platform: 'win32',
        env: {},
        existsSync: () => false,
      }),
    /Packaged Robbie backend executable not found/,
  )
})

test('BackendSidecarManager starts, authenticates, communicates over WS, restarts, and stops cleanly', async () => {
  const states: BackendStatusSnapshot[] = []
  const events: BackendEvent[] = []
  const silentLogger = {
    info: () => undefined,
    warn: () => undefined,
    error: () => undefined,
  }

  const manager = new BackendSidecarManager({
    isPackaged: false,
    appRoot: DESKTOP_ROOT,
    startupTimeoutMs: 15000,
    shutdownTimeoutMs: 5000,
    logger: silentLogger,
  })

  manager.onStatusChange((snapshot) => states.push(snapshot))
  manager.onEvent((event) => events.push(event))

  try {
    const started = await manager.start()
    assert.equal(started.state, 'ready')
    assert.equal(started.host, '127.0.0.1')
    assert.ok(started.port !== null && started.port > 0)
    assert.ok(started.pid !== null && started.pid > 0)
    assert.equal(started.wsConnected, true)
    assert.equal('apiToken' in started, false)
    assert.equal('token' in started, false)

    // Unauthenticated HTTP request must fail with 401
    const unauthResp = await fetch(`http://127.0.0.1:${started.port}/health`)
    assert.equal(unauthResp.status, 401)

    // Untrusted WebSocket origin must be rejected
    const badOriginWs = new LoopbackWebSocketClient({
      host: '127.0.0.1',
      port: started.port,
      path: '/ws/status',
      token: 'not-even-valid-token',
      origin: 'https://evil.example',
    })
    await assert.rejects(() => badOriginWs.connect(), /rejected|403/i)

    // Authenticated health check through manager
    const health = await manager.checkHealth()
    assert.equal(health.status, 'ok')
    assert.equal(health.port, started.port)
    assert.equal(health.websocket_connections, 1)

    // Authenticated WebSocket ping/pong
    const pong = await manager.ping()
    assert.equal(pong.type, 'pong')
    assert.ok(typeof pong.request_id === 'string' && pong.request_id.length > 0)
    assert.ok(events.some((e) => e.type === 'pong'))

    // Restart spawns a fresh sidecar and terminates the previous process
    const firstPid = started.pid
    const restarted = await manager.restart()
    assert.equal(restarted.state, 'ready')
    assert.ok(restarted.pid !== null && restarted.pid !== firstPid)
    assert.equal(isProcessAlive(firstPid), false)

    // Stop cleanly terminates the restarted sidecar without leaving orphans
    const secondPid = restarted.pid
    await manager.stop()
    assert.equal(manager.getSnapshot().state, 'stopped')
    assert.equal(isProcessAlive(secondPid), false)
    assert.ok(states.some((s) => s.state === 'starting'))
    assert.ok(states.some((s) => s.state === 'ready'))
    assert.ok(states.some((s) => s.state === 'stopped'))
  } finally {
    await manager.stop()
  }
})

test('BackendSidecarManager reports startup failure cleanly', async () => {
  const manager = new BackendSidecarManager({
    isPackaged: false,
    appRoot: DESKTOP_ROOT,
    launchConfigOverride: {
      command: process.execPath,
      args: ['-e', 'process.exit(42)'],
      cwd: DESKTOP_ROOT,
      mode: 'override-bin',
    },
    logger: {
      info: () => undefined,
      warn: () => undefined,
      error: () => undefined,
    },
  })

  await assert.rejects(() => manager.start(), /exited before becoming ready \(code=42/)
  const snapshot = manager.getSnapshot()
  assert.equal(snapshot.state, 'error')
  assert.ok(snapshot.lastError?.includes('code=42'))
})
