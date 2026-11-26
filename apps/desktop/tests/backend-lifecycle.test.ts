import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
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

test('BackendSidecarManager handles auth, conversations, settings, emergency stop latch, restart, and cleanup', async () => {
  const states: BackendStatusSnapshot[] = []
  const events: BackendEvent[] = []
  const silentLogger = {
    info: () => undefined,
    warn: () => undefined,
    error: () => undefined,
  }

  const tempDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'robbie-m3-test-'))
  const prevDataDir = process.env.ROBBIE_DATA_DIR
  const prevFakeKey = process.env.OPENAI_API_KEY
  process.env.ROBBIE_DATA_DIR = tempDataDir
  process.env.OPENAI_API_KEY = 'sk-test-never-expose-to-renderer-999999'

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
    assert.equal(started.emergencyStopped, false)
    assert.equal('apiToken' in started, false)
    assert.equal('token' in started, false)

    // Unauthenticated HTTP request must fail with 401
    const unauthResp = await fetch(`http://127.0.0.1:${started.port}/health`)
    assert.equal(unauthResp.status, 401)
    const unauthConv = await fetch(`http://127.0.0.1:${started.port}/conversations`)
    assert.equal(unauthConv.status, 401)

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
    assert.equal(health.emergency_stopped, false)

    // Authenticated WebSocket ping/pong
    const pong = await manager.ping()
    assert.equal(pong.type, 'pong')
    assert.ok(typeof pong.request_id === 'string' && pong.request_id.length > 0)
    assert.ok(events.some((e) => e.type === 'pong'))

    // Conversation CRUD persists across calls
    const created = await manager.createConversation('Milestone 3 Integration')
    assert.equal(created.title, 'Milestone 3 Integration')
    const listed = await manager.listConversations('Milestone')
    assert.equal(listed.length, 1)
    assert.equal(listed[0].conversation_id, created.conversation_id)
    const renamed = await manager.renameConversation(created.conversation_id, 'Renamed Chat')
    assert.equal(renamed.title, 'Renamed Chat')

    // Settings never leak API keys to Electron Main / Renderer
    const settings = await manager.getSettings()
    assert.equal(settings.safety_policy_locked, true)
    const serializedSettings = JSON.stringify(settings)
    assert.equal(
      serializedSettings.includes('sk-test-never-expose-to-renderer-999999'),
      false,
    )

    // Emergency Stop latches persistently until explicit reset
    const stopped = await manager.emergencyStop()
    assert.equal(stopped.status, 'emergency_stopped')
    assert.equal(manager.getSnapshot().emergencyStopped, true)
    const healthWhileStopped = await manager.checkHealth()
    assert.equal(healthWhileStopped.emergency_stopped, true)

    const reset = await manager.resetEmergencyStop()
    assert.equal(reset.emergency_stopped, false)
    assert.equal(manager.getSnapshot().emergencyStopped, false)

    // Delete conversation
    await manager.deleteConversation(created.conversation_id)
    const afterDelete = await manager.listConversations()
    assert.equal(afterDelete.length, 0)

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
    if (prevDataDir === undefined) {
      delete process.env.ROBBIE_DATA_DIR
    } else {
      process.env.ROBBIE_DATA_DIR = prevDataDir
    }
    if (prevFakeKey === undefined) {
      delete process.env.OPENAI_API_KEY
    } else {
      process.env.OPENAI_API_KEY = prevFakeKey
    }
    fs.rmSync(tempDataDir, { recursive: true, force: true })
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

test('BackendSidecarManager drains > 64 KB of stdout after readiness without deadlocking', async () => {
  const mockServerScript = `
const http = require('node:http');
const token = process.env.ROBBIE_API_TOKEN;
const server = http.createServer((req, res) => {
  if (req.headers.authorization !== 'Bearer ' + token) {
    res.writeHead(401);
    res.end();
    return;
  }
  if (req.url === '/health') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      status: 'ok',
      service: 'robbie-backend',
      version: '0.1.0',
      host: '127.0.0.1',
      port: server.address().port,
      pid: process.pid,
      uptime_seconds: 1.0,
      websocket_connections: 0,
      emergency_stopped: false,
      timestamp: new Date().toISOString(),
    }));
    return;
  }
  if (req.url === '/conversations') {
    // Emit > 128 KB to stdout after readiness to verify the parent keeps draining the pipe.
    const line = 'Agent-S stdout diagnostic payload ' + 'X'.repeat(200) + '\\n';
    for (let i = 0; i < 650; i++) {
      process.stdout.write(line);
    }
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end('[]');
    return;
  }
  res.writeHead(404);
  res.end();
});
server.on('upgrade', (_req, socket) => {
  socket.write('HTTP/1.1 101 Switching Protocols\\r\\nUpgrade: websocket\\r\\nConnection: Upgrade\\r\\n');
  const crypto = require('node:crypto');
  const key = _req.headers['sec-websocket-key'];
  const accept = crypto.createHash('sha1').update(key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest('base64');
  socket.write('Sec-WebSocket-Accept: ' + accept + '\\r\\n\\r\\n');
});
server.listen(0, '127.0.0.1', () => {
  const port = server.address().port;
  process.stdout.write(JSON.stringify({
    event: 'robbie_backend_ready',
    host: '127.0.0.1',
    port,
    pid: process.pid,
    version: '0.1.0',
  }) + '\\n');
});
process.stdin.resume();
process.stdin.on('end', () => { server.close(() => process.exit(0)); });
`

  const manager = new BackendSidecarManager({
    isPackaged: false,
    appRoot: DESKTOP_ROOT,
    startupTimeoutMs: 10000,
    shutdownTimeoutMs: 5000,
    launchConfigOverride: {
      command: process.execPath,
      args: ['-e', mockServerScript],
      cwd: DESKTOP_ROOT,
      mode: 'override-bin',
    },
    logger: {
      info: () => undefined,
      warn: () => undefined,
      error: () => undefined,
    },
  })

  try {
    const started = await manager.start()
    assert.equal(started.state, 'ready')

    // Triggers > 128 KB synchronous stdout writes inside the child sidecar
    const convs = await manager.listConversations()
    assert.deepEqual(convs, [])

    // Child must remain responsive after heavy stdout emission (zero pipe deadlock)
    const health = await manager.checkHealth()
    assert.equal(health.status, 'ok')
  } finally {
    await manager.stop()
  }
})
