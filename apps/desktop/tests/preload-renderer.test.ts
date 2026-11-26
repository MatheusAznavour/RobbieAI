import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { RobbieRendererClient } from '../src/lib/robbieClient.ts'
import type {
  BackendEvent,
  BackendHealthResponse,
  BackendPongEvent,
  BackendStatusSnapshot,
  RobbieApi,
} from '../shared/backend.ts'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const DESKTOP_ROOT = path.resolve(__dirname, '..')

test('RobbieRendererClient delegates strictly through scoped window.robbie bridge', async () => {
  const statusListeners = new Set<(snapshot: BackendStatusSnapshot) => void>()
  const eventListeners = new Set<(event: BackendEvent) => void>()

  const readySnapshot: BackendStatusSnapshot = {
    state: 'ready',
    host: '127.0.0.1',
    port: 54321,
    pid: 1234,
    version: '0.1.0',
    wsConnected: true,
    lastError: null,
    updatedAt: '2026-11-26T00:00:00.000Z',
  }

  const healthPayload: BackendHealthResponse = {
    status: 'ok',
    service: 'robbie-backend',
    version: '0.1.0',
    host: '127.0.0.1',
    port: 54321,
    pid: 1234,
    uptime_seconds: 1.5,
    websocket_connections: 1,
    timestamp: '2026-11-26T00:00:01.000Z',
  }

  const pongPayload: BackendPongEvent = {
    type: 'pong',
    request_id: 'req-1',
    timestamp: '2026-11-26T00:00:02.000Z',
  }

  const mockBridge: RobbieApi = {
    getBackendStatus: async () => readySnapshot,
    checkHealth: async () => healthPayload,
    pingBackend: async () => pongPayload,
    restartBackend: async () => readySnapshot,
    onBackendStatusChange: (listener) => {
      statusListeners.add(listener)
      return () => statusListeners.delete(listener)
    },
    onBackendEvent: (listener) => {
      eventListeners.add(listener)
      return () => eventListeners.delete(listener)
    },
  }

  const client = new RobbieRendererClient(() => mockBridge)

  assert.deepEqual(await client.getBackendStatus(), readySnapshot)
  assert.deepEqual(await client.checkHealth(), healthPayload)
  assert.deepEqual(await client.pingBackend(), pongPayload)
  assert.deepEqual(await client.restartBackend(), readySnapshot)

  const seenStatuses: BackendStatusSnapshot[] = []
  const unsubStatus = client.onBackendStatusChange((s) => seenStatuses.push(s))
  for (const listener of statusListeners) listener(readySnapshot)
  assert.equal(seenStatuses.length, 1)
  unsubStatus()
  assert.equal(statusListeners.size, 0)

  const seenEvents: BackendEvent[] = []
  const unsubEvent = client.onBackendEvent((e) => seenEvents.push(e))
  for (const listener of eventListeners) listener(pongPayload)
  assert.equal(seenEvents.length, 1)
  unsubEvent()
  assert.equal(eventListeners.size, 0)
})

test('RobbieRendererClient fails cleanly when window.robbie is absent', () => {
  const client = new RobbieRendererClient(() => undefined)
  assert.throws(() => client.getBackendStatus(), /window\.robbie/)
})

test('Electron security hardening invariants hold in source configuration', () => {
  const preloadSrc = fs.readFileSync(
    path.join(DESKTOP_ROOT, 'electron', 'preload.ts'),
    'utf8',
  )
  const mainSrc = fs.readFileSync(
    path.join(DESKTOP_ROOT, 'electron', 'main.ts'),
    'utf8',
  )
  const managerSrc = fs.readFileSync(
    path.join(DESKTOP_ROOT, 'electron', 'backend', 'manager.ts'),
    'utf8',
  )

  // Preload must expose only `robbie`, never raw `ipcRenderer`
  assert.ok(preloadSrc.includes("contextBridge.exposeInMainWorld('robbie'"))
  assert.equal(preloadSrc.includes("exposeInMainWorld('ipcRenderer'"), false)

  // Main must enforce isolation and sandboxing
  assert.ok(mainSrc.includes('contextIsolation: true'))
  assert.ok(mainSrc.includes('nodeIntegration: false'))
  assert.ok(mainSrc.includes('sandbox: true'))
  assert.ok(mainSrc.includes('Content-Security-Policy'))

  // Backend sidecar must never bind 0.0.0.0 or use shell: true
  assert.equal(managerSrc.includes('0.0.0.0'), false)
  assert.ok(managerSrc.includes('shell: false'))
})
