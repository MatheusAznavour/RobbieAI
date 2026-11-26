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
  ConversationDetail,
  ConversationSummary,
  EmergencyStopResetResponse,
  EmergencyStopResponse,
  ModelSelectionState,
  RobbieApi,
  SendMessageResponse,
  SettingsSnapshot,
  TaskRecordView,
} from '../shared/backend.ts'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const DESKTOP_ROOT = path.resolve(__dirname, '..')

function collectSourceFiles(dir: string): string[] {
  if (!fs.existsSync(dir)) return []
  const out: string[] = []
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name)
    if (entry.isDirectory()) {
      out.push(...collectSourceFiles(full))
    } else if (entry.name.endsWith('.ts') || entry.name.endsWith('.tsx')) {
      out.push(full)
    }
  }
  return out
}

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
    emergencyStopped: false,
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
    emergency_stopped: false,
    timestamp: '2026-11-26T00:00:01.000Z',
  }

  const pongPayload: BackendPongEvent = {
    type: 'pong',
    request_id: 'req-1',
    timestamp: '2026-11-26T00:00:02.000Z',
  }

  const summary: ConversationSummary = {
    conversation_id: 'conv-1',
    title: 'Hello Robbie',
    created_at: '2026-11-26T00:00:00.000Z',
    updated_at: '2026-11-26T00:00:00.000Z',
    message_count: 2,
    task_count: 0,
    last_message_preview: 'Hello!',
  }

  const detail: ConversationDetail = {
    conversation_id: 'conv-1',
    title: 'Hello Robbie',
    created_at: '2026-11-26T00:00:00.000Z',
    updated_at: '2026-11-26T00:00:00.000Z',
    messages: [],
    task_ids: [],
    tasks: [],
  }

  const taskView: TaskRecordView = {
    task_id: 'task-1',
    conversation_id: 'conv-1',
    instruction: 'Open Notepad',
    state: 'running',
    current_step: 1,
    max_steps: 10,
    last_step_id: 'step-1',
    last_error: null,
    created_at: '2026-11-26T00:00:00.000Z',
    updated_at: '2026-11-26T00:00:00.000Z',
    pending_approval: null,
    events: [],
  }

  const sendResponse: SendMessageResponse = {
    conversation: summary,
    user_message: {
      message_id: 'm1',
      conversation_id: 'conv-1',
      role: 'user',
      content: 'Hello',
      kind: 'chat',
      task_id: null,
      created_at: '2026-11-26T00:00:00.000Z',
    },
    assistant_message: {
      message_id: 'm2',
      conversation_id: 'conv-1',
      role: 'assistant',
      content: 'Hi there!',
      kind: 'chat',
      task_id: null,
      created_at: '2026-11-26T00:00:01.000Z',
    },
    task: null,
  }

  const selection: ModelSelectionState = {
    chat_provider: 'openai',
    chat_model: 'gpt-4o',
    chat_base_url: null,
    worker_provider: 'openai',
    worker_model: 'gpt-4o',
    worker_base_url: null,
    grounding_provider: 'huggingface',
    grounding_model: 'ui-tars-1.5-7b',
    grounding_url: '',
    grounding_width: 1920,
    grounding_height: 1080,
    max_steps: 15,
    enable_reflection: true,
  }

  const settings: SettingsSnapshot = {
    selection,
    providers: [],
    chat_ready: false,
    chat_configuration_errors: ['Missing OPENAI_API_KEY'],
    desktop_ready: false,
    desktop_configuration_errors: ['Missing grounding URL'],
    safety_policy_locked: true,
    requires_restart_on_env_change: true,
  }

  const stopResp: EmergencyStopResponse = {
    status: 'emergency_stopped',
    affected_task_ids: ['task-1'],
    timestamp: '2026-11-26T00:00:05.000Z',
  }

  const resetResp: EmergencyStopResetResponse = {
    status: 'ready',
    emergency_stopped: false,
    timestamp: '2026-11-26T00:00:06.000Z',
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
    listConversations: async () => [summary],
    createConversation: async () => detail,
    getConversation: async () => detail,
    renameConversation: async () => summary,
    deleteConversation: async () => undefined,
    sendMessage: async () => sendResponse,
    retryMessage: async () => sendResponse,
    listTasks: async () => [taskView],
    getTask: async () => taskView,
    pauseTask: async () => taskView,
    resumeTask: async () => taskView,
    cancelTask: async () => taskView,
    resolveApproval: async () => taskView,
    emergencyStop: async () => stopResp,
    resetEmergencyStop: async () => resetResp,
    getSettings: async () => settings,
    updateSettings: async () => settings,
  }

  const client = new RobbieRendererClient(() => mockBridge)

  assert.deepEqual(await client.getBackendStatus(), readySnapshot)
  assert.deepEqual(await client.checkHealth(), healthPayload)
  assert.deepEqual(await client.pingBackend(), pongPayload)
  assert.deepEqual(await client.restartBackend(), readySnapshot)
  assert.deepEqual(await client.listConversations(), [summary])
  assert.deepEqual(await client.createConversation('New'), detail)
  assert.deepEqual(await client.getConversation('conv-1'), detail)
  assert.deepEqual(await client.renameConversation('conv-1', 'Renamed'), summary)
  assert.equal(await client.deleteConversation('conv-1'), undefined)
  assert.deepEqual(await client.sendMessage('conv-1', 'Hello', 'chat'), sendResponse)
  assert.deepEqual(await client.retryMessage('conv-1', 'm1'), sendResponse)
  assert.deepEqual(await client.listTasks(), [taskView])
  assert.deepEqual(await client.getTask('task-1'), taskView)
  assert.deepEqual(await client.pauseTask('task-1'), taskView)
  assert.deepEqual(await client.resumeTask('task-1'), taskView)
  assert.deepEqual(await client.cancelTask('task-1'), taskView)
  assert.deepEqual(
    await client.resolveApproval('task-1', 'ap-1', {
      approved: true,
      step_id: 'step-1',
      action_digest: 'a'.repeat(64),
    }),
    taskView,
  )
  assert.deepEqual(await client.emergencyStop(), stopResp)
  assert.deepEqual(await client.resetEmergencyStop(), resetResp)
  assert.deepEqual(await client.getSettings(), settings)
  assert.deepEqual(await client.updateSettings(selection), settings)

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

test('Electron security hardening and zero-HTML-injection invariants hold in source', () => {
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

  // Preload must expose only `robbie`, never raw `ipcRenderer` or tokens
  assert.ok(preloadSrc.includes("contextBridge.exposeInMainWorld('robbie'"))
  assert.equal(preloadSrc.includes("exposeInMainWorld('ipcRenderer'"), false)
  assert.equal(preloadSrc.includes('apiToken'), false)
  assert.equal(preloadSrc.includes('OPENAI_API_KEY'), false)

  // Main must enforce isolation, sandboxing, CSP, and global emergency stop
  assert.ok(mainSrc.includes('contextIsolation: true'))
  assert.ok(mainSrc.includes('nodeIntegration: false'))
  assert.ok(mainSrc.includes('sandbox: true'))
  assert.ok(mainSrc.includes('Content-Security-Policy'))
  assert.ok(mainSrc.includes('globalShortcut'))

  // Backend sidecar must never bind 0.0.0.0 or use shell: true
  assert.equal(managerSrc.includes('0.0.0.0'), false)
  assert.ok(managerSrc.includes('shell: false'))

  // Renderer source must NEVER use dangerouslySetInnerHTML or direct fetch to backend
  const rendererFiles = collectSourceFiles(path.join(DESKTOP_ROOT, 'src'))
  for (const file of rendererFiles) {
    const src = fs.readFileSync(file, 'utf8')
    assert.equal(
      src.includes('dangerouslySetInnerHTML'),
      false,
      `Forbidden dangerouslySetInnerHTML found in ${file}`,
    )
    assert.equal(
      src.includes('innerHTML'),
      false,
      `Forbidden innerHTML found in ${file}`,
    )
    assert.equal(
      src.includes('ipcRenderer'),
      false,
      `Forbidden ipcRenderer reference found in ${file}`,
    )
  }
})
