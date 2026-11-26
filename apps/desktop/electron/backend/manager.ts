import { execFile, spawn, type ChildProcess } from 'node:child_process'
import crypto from 'node:crypto'
import type {
  ApprovalDecisionPayload,
  BackendEvent,
  BackendHealthResponse,
  BackendLifecycleState,
  BackendPongEvent,
  BackendStatusSnapshot,
  ChatMode,
  ConversationDetail,
  ConversationSummary,
  EmergencyStopResetResponse,
  EmergencyStopResponse,
  ModelSelectionState,
  SendMessageResponse,
  SettingsSnapshot,
  TaskRecordView,
} from '../../shared/backend.ts'
import { BackendClient, TRUSTED_BACKEND_ORIGIN } from './client.ts'
import {
  resolveBackendLaunchConfig,
  type BackendLaunchConfig,
} from './paths.ts'

interface ReadyStdoutMessage {
  event: 'robbie_backend_ready'
  host: string
  port: number
  pid: number
  version: string
}

export interface BackendSidecarManagerOptions {
  isPackaged: boolean
  appRoot: string
  resourcesPath?: string
  startupTimeoutMs?: number
  shutdownTimeoutMs?: number
  launchConfigOverride?: BackendLaunchConfig
  logger?: Pick<Console, 'info' | 'warn' | 'error'>
}

function nowIso(): string {
  return new Date().toISOString()
}

function sanitizeText(text: string, secretToken: string | null): string {
  if (!secretToken) {
    return text
  }
  return text.split(secretToken).join('[REDACTED]')
}

function parseReadyLine(line: string): ReadyStdoutMessage | null {
  const trimmed = line.trim()
  if (!trimmed.startsWith('{') || !trimmed.endsWith('}')) {
    return null
  }
  try {
    const parsed = JSON.parse(trimmed) as Partial<ReadyStdoutMessage>
    if (
      parsed.event === 'robbie_backend_ready' &&
      parsed.host === '127.0.0.1' &&
      typeof parsed.port === 'number' &&
      parsed.port > 0 &&
      typeof parsed.pid === 'number' &&
      typeof parsed.version === 'string'
    ) {
      return parsed as ReadyStdoutMessage
    }
  } catch {
    return null
  }
  return null
}

function terminateWindowsProcessTree(pid: number): Promise<void> {
  return new Promise((resolve) => {
    execFile(
      'taskkill.exe',
      ['/PID', String(pid), '/T', '/F'],
      { windowsHide: true },
      () => {
        resolve()
      },
    )
  })
}

/**
 * Owns the Python FastAPI sidecar process lifecycle, per-launch session bearer
 * token, dynamic loopback port capture, readiness verification, and cleanup.
 */
export class BackendSidecarManager {
  private readonly options: BackendSidecarManagerOptions
  private child: ChildProcess | null = null
  private client: BackendClient | null = null
  private sessionToken: string | null = null
  private startPromise: Promise<BackendStatusSnapshot> | null = null
  private stopPromise: Promise<void> | null = null
  private intentionalStop = false
  private readonly statusListeners = new Set<(snapshot: BackendStatusSnapshot) => void>()
  private readonly eventListeners = new Set<(event: BackendEvent) => void>()
  private readonly logger: Pick<Console, 'info' | 'warn' | 'error'>

  private snapshot: BackendStatusSnapshot = {
    state: 'stopped',
    host: null,
    port: null,
    pid: null,
    version: null,
    wsConnected: false,
    emergencyStopped: false,
    lastError: null,
    updatedAt: nowIso(),
  }

  constructor(options: BackendSidecarManagerOptions) {
    this.options = options
    this.logger = options.logger ?? console
  }

  getSnapshot(): BackendStatusSnapshot {
    return { ...this.snapshot }
  }

  onStatusChange(listener: (snapshot: BackendStatusSnapshot) => void): () => void {
    this.statusListeners.add(listener)
    return () => {
      this.statusListeners.delete(listener)
    }
  }

  onEvent(listener: (event: BackendEvent) => void): () => void {
    this.eventListeners.add(listener)
    return () => {
      this.eventListeners.delete(listener)
    }
  }

  private requireClient(): BackendClient {
    if (!this.client || this.snapshot.state !== 'ready') {
      throw new Error('Robbie backend is not ready.')
    }
    return this.client
  }

  async start(): Promise<BackendStatusSnapshot> {
    if (this.snapshot.state === 'ready' && this.child && this.client) {
      return this.getSnapshot()
    }
    if (this.startPromise) {
      return this.startPromise
    }

    this.startPromise = this.performStart().finally(() => {
      this.startPromise = null
    })
    return this.startPromise
  }

  async stop(): Promise<void> {
    if (this.stopPromise) {
      return this.stopPromise
    }

    this.stopPromise = this.performStop().finally(() => {
      this.stopPromise = null
    })
    return this.stopPromise
  }

  async restart(): Promise<BackendStatusSnapshot> {
    this.updateSnapshot({ state: 'restarting', lastError: null })
    await this.stop()
    return this.start()
  }

  async checkHealth(): Promise<BackendHealthResponse> {
    const health = await this.requireClient().checkHealth()
    if (this.snapshot.emergencyStopped !== health.emergency_stopped) {
      this.updateSnapshot({ emergencyStopped: health.emergency_stopped })
    }
    return health
  }

  ping(): Promise<BackendPongEvent> {
    return this.requireClient().ping()
  }

  listConversations(query?: string): Promise<ConversationSummary[]> {
    return this.requireClient().listConversations(query)
  }

  createConversation(title?: string): Promise<ConversationDetail> {
    return this.requireClient().createConversation(title)
  }

  getConversation(conversationId: string): Promise<ConversationDetail> {
    return this.requireClient().getConversation(conversationId)
  }

  renameConversation(
    conversationId: string,
    title: string,
  ): Promise<ConversationSummary> {
    return this.requireClient().renameConversation(conversationId, title)
  }

  deleteConversation(conversationId: string): Promise<void> {
    return this.requireClient().deleteConversation(conversationId)
  }

  sendMessage(
    conversationId: string,
    content: string,
    mode?: ChatMode,
  ): Promise<SendMessageResponse> {
    return this.requireClient().sendMessage(conversationId, content, mode)
  }

  retryMessage(
    conversationId: string,
    messageId: string,
  ): Promise<SendMessageResponse> {
    return this.requireClient().retryMessage(conversationId, messageId)
  }

  listTasks(): Promise<TaskRecordView[]> {
    return this.requireClient().listTasks()
  }

  getTask(taskId: string): Promise<TaskRecordView> {
    return this.requireClient().getTask(taskId)
  }

  pauseTask(taskId: string): Promise<TaskRecordView> {
    return this.requireClient().pauseTask(taskId)
  }

  resumeTask(taskId: string): Promise<TaskRecordView> {
    return this.requireClient().resumeTask(taskId)
  }

  cancelTask(taskId: string): Promise<TaskRecordView> {
    return this.requireClient().cancelTask(taskId)
  }

  resolveApproval(
    taskId: string,
    approvalId: string,
    decision: ApprovalDecisionPayload,
  ): Promise<TaskRecordView> {
    return this.requireClient().resolveApproval(taskId, approvalId, decision)
  }

  async emergencyStop(): Promise<EmergencyStopResponse> {
    const res = await this.requireClient().emergencyStop()
    this.updateSnapshot({ emergencyStopped: true })
    return res
  }

  async resetEmergencyStop(): Promise<EmergencyStopResetResponse> {
    const res = await this.requireClient().resetEmergencyStop()
    this.updateSnapshot({ emergencyStopped: false })
    return res
  }

  getSettings(): Promise<SettingsSnapshot> {
    return this.requireClient().getSettings()
  }

  updateSettings(selection: ModelSelectionState): Promise<SettingsSnapshot> {
    return this.requireClient().updateSettings(selection)
  }

  private async performStart(): Promise<BackendStatusSnapshot> {
    this.intentionalStop = false
    this.updateSnapshot({
      state: 'starting',
      host: '127.0.0.1',
      port: null,
      pid: null,
      version: null,
      wsConnected: false,
      emergencyStopped: false,
      lastError: null,
    })

    let launchConfig: BackendLaunchConfig
    try {
      launchConfig =
        this.options.launchConfigOverride ??
        resolveBackendLaunchConfig({
          isPackaged: this.options.isPackaged,
          appRoot: this.options.appRoot,
          resourcesPath: this.options.resourcesPath,
        })
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      this.updateSnapshot({ state: 'error', lastError: message })
      throw err
    }

    const token = crypto.randomBytes(32).toString('base64url')
    this.sessionToken = token

    const childEnv: NodeJS.ProcessEnv = {
      ...process.env,
      ROBBIE_BACKEND_HOST: '127.0.0.1',
      ROBBIE_BACKEND_PORT: '0',
      ROBBIE_API_TOKEN: token,
      ROBBIE_ALLOWED_ORIGINS: TRUSTED_BACKEND_ORIGIN,
      ROBBIE_WATCH_STDIN: '1',
      PYTHONUNBUFFERED: '1',
    }

    const child = spawn(launchConfig.command, launchConfig.args, {
      cwd: launchConfig.cwd,
      env: childEnv,
      shell: false,
      windowsHide: true,
      stdio: ['pipe', 'pipe', 'pipe'],
    })
    this.child = child

    const stderrTail: string[] = []
    const stdoutTail: string[] = []
    let readyInfo: ReadyStdoutMessage

    try {
      readyInfo = await this.waitForReadyStdout(
        child,
        token,
        stdoutTail,
        stderrTail,
        this.options.startupTimeoutMs ?? 15000,
      )

      const client = new BackendClient({
        host: '127.0.0.1',
        port: readyInfo.port,
        token,
        origin: TRUSTED_BACKEND_ORIGIN,
        autoReconnect: true,
        onEvent: (event) => {
          if (event.type === 'emergency_stop') {
            this.updateSnapshot({ emergencyStopped: true })
          } else if (event.type === 'emergency_stop_reset') {
            this.updateSnapshot({ emergencyStopped: false })
          } else if (event.type === 'status' && 'emergency_stopped' in event) {
            this.updateSnapshot({ emergencyStopped: Boolean(event.emergency_stopped) })
          }
          this.emitEvent(event)
        },
        onWebSocketStateChange: (connected) => {
          if (this.snapshot.wsConnected !== connected) {
            this.updateSnapshot({ wsConnected: connected })
          }
        },
        onError: (err) => {
          this.logger.warn(
            '[RobbieBackend] WebSocket error:',
            sanitizeText(err.message, this.sessionToken),
          )
        },
      })
      this.client = client

      const health = await client.checkHealth()
      await client.connectWebSocket()

      this.attachRuntimeExitHandler(child, stderrTail)
      this.updateSnapshot({
        state: 'ready',
        host: health.host,
        port: health.port,
        pid: health.pid,
        version: health.version,
        wsConnected: client.isWebSocketConnected,
        emergencyStopped: health.emergency_stopped,
        lastError: null,
      })

      this.logger.info(
        `[RobbieBackend] Ready on ${health.host}:${health.port} (pid=${health.pid}, mode=${launchConfig.mode})`,
      )
      return this.getSnapshot()
    } catch (err) {
      const rawMessage = err instanceof Error ? err.message : String(err)
      const safeMessage = sanitizeText(rawMessage, token)
      await this.forceCleanupFailedStart(child)
      this.updateSnapshot({
        state: 'error',
        port: null,
        pid: null,
        wsConnected: false,
        lastError: safeMessage,
      })
      throw new Error(safeMessage)
    }
  }

  private waitForReadyStdout(
    child: ChildProcess,
    token: string,
    stdoutTail: string[],
    stderrTail: string[],
    timeoutMs: number,
  ): Promise<ReadyStdoutMessage> {
    return new Promise((resolve, reject) => {
      let stdoutBuffer = ''
      let settled = false
      let readyResolved = false

      const appendBoundedLine = (target: string[], raw: string, maxLines: number) => {
        const sanitized = sanitizeText(raw, token).trim()
        if (!sanitized) return
        const bounded =
          sanitized.length > 1024 ? `${sanitized.slice(0, 1021)}...` : sanitized
        target.push(bounded)
        if (target.length > maxLines) {
          target.shift()
        }
      }

      const finish = (fn: () => void) => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        // Keep `onStdout` and `onStderr` attached for the child's full lifetime so
        // stdout/stderr pipes are continuously drained and never stall Python on Windows.
        child.off('error', onError)
        child.off('exit', onExit)
        fn()
      }

      const timer = setTimeout(() => {
        finish(() => {
          const detail = stderrTail.length > 0 ? ` Stderr: ${stderrTail.join(' | ')}` : ''
          reject(
            new Error(
              `Timed out waiting ${timeoutMs}ms for Robbie backend readiness.${detail}`,
            ),
          )
        })
      }, timeoutMs)

      const onStdout = (chunk: Buffer) => {
        const text = chunk.toString('utf8')
        if (readyResolved) {
          appendBoundedLine(stdoutTail, text, 40)
          return
        }

        stdoutBuffer += text
        let newlineIdx = stdoutBuffer.indexOf('\n')
        while (newlineIdx !== -1) {
          const line = stdoutBuffer.slice(0, newlineIdx)
          stdoutBuffer = stdoutBuffer.slice(newlineIdx + 1)
          const parsed = parseReadyLine(line)
          if (parsed) {
            readyResolved = true
            if (stdoutBuffer.trim()) {
              appendBoundedLine(stdoutTail, stdoutBuffer, 40)
            }
            stdoutBuffer = ''
            finish(() => resolve(parsed))
            return
          }
          appendBoundedLine(stdoutTail, line, 40)
          newlineIdx = stdoutBuffer.indexOf('\n')
        }
        // Prevent unbounded buffer growth if stdout emits large lines before readiness.
        if (stdoutBuffer.length > 65536) {
          appendBoundedLine(stdoutTail, stdoutBuffer.slice(0, 1024), 40)
          stdoutBuffer = stdoutBuffer.slice(-4096)
        }
      }

      const onStderr = (chunk: Buffer) => {
        appendBoundedLine(stderrTail, chunk.toString('utf8'), 40)
      }

      const onError = (err: Error) => {
        finish(() => reject(new Error(`Failed to spawn backend process: ${err.message}`)))
      }

      const onExit = (code: number | null, signal: NodeJS.Signals | null) => {
        finish(() => {
          const detail = stderrTail.length > 0 ? ` Stderr: ${stderrTail.join(' | ')}` : ''
          reject(
            new Error(
              `Backend exited before becoming ready (code=${code}, signal=${signal}).${detail}`,
            ),
          )
        })
      }

      child.stdout?.on('data', onStdout)
      child.stderr?.on('data', onStderr)
      child.once('error', onError)
      child.once('exit', onExit)
      child.once('close', () => {
        child.stdout?.off('data', onStdout)
        child.stderr?.off('data', onStderr)
      })
    })
  }

  private attachRuntimeExitHandler(child: ChildProcess, stderrTail: string[]): void {
    child.once('exit', (code, signal) => {
      if (this.child !== child) {
        return
      }
      this.child = null
      this.client?.disconnect()
      this.client = null
      const tokenForSanitize = this.sessionToken
      this.sessionToken = null

      if (this.intentionalStop) {
        this.updateSnapshot({
          state: 'stopped',
          port: null,
          pid: null,
          wsConnected: false,
          lastError: null,
        })
        return
      }

      const detail =
        stderrTail.length > 0
          ? ` (${sanitizeText(stderrTail[stderrTail.length - 1], tokenForSanitize)})`
          : ''
      const message = `Backend exited unexpectedly (code=${code}, signal=${signal})${detail}.`
      this.logger.error(`[RobbieBackend] ${message}`)
      this.updateSnapshot({
        state: 'error',
        port: null,
        pid: null,
        wsConnected: false,
        lastError: message,
      })
    })
  }

  private async forceCleanupFailedStart(child: ChildProcess): Promise<void> {
    this.intentionalStop = true
    this.client?.disconnect()
    this.client = null
    if (this.child === child) {
      this.child = null
    }
    this.sessionToken = null
    await this.terminateChild(child, 2000)
  }

  private async performStop(): Promise<void> {
    this.intentionalStop = true
    const child = this.child
    this.client?.disconnect()
    this.client = null
    this.sessionToken = null

    if (!child) {
      this.updateSnapshot({
        state: 'stopped',
        port: null,
        pid: null,
        wsConnected: false,
      })
      return
    }

    this.updateSnapshot({ state: 'stopping', wsConnected: false })
    await this.terminateChild(child, this.options.shutdownTimeoutMs ?? 3000)

    if (this.child === child) {
      this.child = null
    }
    this.updateSnapshot({
      state: 'stopped',
      port: null,
      pid: null,
      wsConnected: false,
      lastError: null,
    })
  }

  private async terminateChild(child: ChildProcess, graceTimeoutMs: number): Promise<void> {
    if (child.exitCode !== null || child.killed) {
      return
    }

    const exitPromise = new Promise<void>((resolve) => {
      child.once('exit', () => resolve())
    })

    try {
      if (child.stdin && !child.stdin.destroyed) {
        child.stdin.end()
      }
    } catch {
      // Ignore pipe errors if already closed.
    }

    if (process.platform !== 'win32') {
      try {
        child.kill('SIGTERM')
      } catch {
        // Ignore if already exited.
      }
    }

    const exitedGracefully = await Promise.race([
      exitPromise.then(() => true),
      new Promise<boolean>((resolve) => setTimeout(() => resolve(false), graceTimeoutMs)),
    ])

    if (exitedGracefully || child.exitCode !== null) {
      return
    }

    if (process.platform === 'win32' && child.pid) {
      await terminateWindowsProcessTree(child.pid)
    }
    try {
      child.kill('SIGKILL')
    } catch {
      // Process may already be terminated.
    }

    await Promise.race([
      exitPromise,
      new Promise<void>((resolve) => setTimeout(resolve, 2000)),
    ])
  }

  private updateSnapshot(partial: Partial<BackendStatusSnapshot>): void {
    const nextState: BackendLifecycleState = partial.state ?? this.snapshot.state
    this.snapshot = {
      ...this.snapshot,
      ...partial,
      state: nextState,
      updatedAt: nowIso(),
    }
    const copy = this.getSnapshot()
    for (const listener of this.statusListeners) {
      try {
        listener(copy)
      } catch {
        // Isolate listener failures.
      }
    }
  }

  private emitEvent(event: BackendEvent): void {
    for (const listener of this.eventListeners) {
      try {
        listener(event)
      } catch {
        // Isolate listener failures.
      }
    }
  }
}
