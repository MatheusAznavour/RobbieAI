import crypto from 'node:crypto'
import type {
  ApprovalDecisionPayload,
  BackendEvent,
  BackendHealthResponse,
  BackendPongEvent,
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
import { LoopbackWebSocketClient } from './wsClient.ts'

export const TRUSTED_BACKEND_ORIGIN = 'robbie://desktop'

export interface BackendClientOptions {
  host?: '127.0.0.1'
  port: number
  token: string
  origin?: string
  autoReconnect?: boolean
  onEvent?: (event: BackendEvent) => void
  onWebSocketStateChange?: (connected: boolean) => void
  onError?: (error: Error) => void
}

interface PendingPing {
  resolve: (event: BackendPongEvent) => void
  reject: (error: Error) => void
  timer: NodeJS.Timeout
}

/**
 * Typed authenticated REST + WebSocket client for communicating with the local
 * Robbie FastAPI backend strictly from the Electron Main process.
 */
export class BackendClient {
  private readonly options: BackendClientOptions
  private readonly host: '127.0.0.1'
  private readonly port: number
  private readonly token: string
  private readonly origin: string
  private ws: LoopbackWebSocketClient | null = null
  private reconnectTimer: NodeJS.Timeout | null = null
  private reconnectAttempt = 0
  private manuallyDisconnected = false
  private readonly pendingPings = new Map<string, PendingPing>()

  constructor(options: BackendClientOptions) {
    this.options = options
    this.host = options.host ?? '127.0.0.1'
    this.port = options.port
    this.token = options.token
    this.origin = options.origin ?? TRUSTED_BACKEND_ORIGIN
  }

  get isWebSocketConnected(): boolean {
    return this.ws?.isConnected ?? false
  }

  private async requestJson<T>(
    method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE',
    endpointPath: string,
    body?: unknown,
    timeoutMs = 15000,
  ): Promise<T> {
    const headers: Record<string, string> = {
      Authorization: `Bearer ${this.token}`,
      Origin: this.origin,
    }
    let serializedBody: string | undefined
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json'
      serializedBody = JSON.stringify(body)
    }

    const response = await fetch(`http://${this.host}:${this.port}${endpointPath}`, {
      method,
      headers,
      body: serializedBody,
      signal: AbortSignal.timeout(timeoutMs),
    })

    if (!response.ok) {
      let detail = `Backend request failed with HTTP ${response.status}.`
      try {
        const errJson = (await response.json()) as { detail?: unknown }
        if (typeof errJson.detail === 'string' && errJson.detail.trim()) {
          detail = `HTTP ${response.status}: ${errJson.detail}`
        }
      } catch {
        // Use generic status message.
      }
      throw new Error(detail)
    }

    if (response.status === 204) {
      return undefined as T
    }

    return (await response.json()) as T
  }

  async checkHealth(timeoutMs = 5000): Promise<BackendHealthResponse> {
    const payload = await this.requestJson<Partial<BackendHealthResponse>>(
      'GET',
      '/health',
      undefined,
      timeoutMs,
    )
    if (payload.status !== 'ok' || payload.service !== 'robbie-backend') {
      throw new Error('Backend /health returned an unexpected response payload.')
    }
    return payload as BackendHealthResponse
  }

  listConversations(query = ''): Promise<ConversationSummary[]> {
    const suffix = query.trim() ? `?q=${encodeURIComponent(query.trim())}` : ''
    return this.requestJson<ConversationSummary[]>('GET', `/conversations${suffix}`)
  }

  createConversation(title?: string): Promise<ConversationDetail> {
    return this.requestJson<ConversationDetail>(
      'POST',
      '/conversations',
      title ? { title } : {},
    )
  }

  getConversation(conversationId: string): Promise<ConversationDetail> {
    return this.requestJson<ConversationDetail>(
      'GET',
      `/conversations/${encodeURIComponent(conversationId)}`,
    )
  }

  renameConversation(
    conversationId: string,
    title: string,
  ): Promise<ConversationSummary> {
    return this.requestJson<ConversationSummary>(
      'PATCH',
      `/conversations/${encodeURIComponent(conversationId)}`,
      { title },
    )
  }

  deleteConversation(conversationId: string): Promise<void> {
    return this.requestJson<void>(
      'DELETE',
      `/conversations/${encodeURIComponent(conversationId)}`,
    )
  }

  sendMessage(
    conversationId: string,
    content: string,
    mode: ChatMode = 'auto',
  ): Promise<SendMessageResponse> {
    return this.requestJson<SendMessageResponse>(
      'POST',
      `/conversations/${encodeURIComponent(conversationId)}/messages`,
      { content, mode },
      100000,
    )
  }

  retryMessage(
    conversationId: string,
    messageId: string,
  ): Promise<SendMessageResponse> {
    return this.requestJson<SendMessageResponse>(
      'POST',
      `/conversations/${encodeURIComponent(conversationId)}/messages/${encodeURIComponent(messageId)}/retry`,
      undefined,
      100000,
    )
  }

  listTasks(): Promise<TaskRecordView[]> {
    return this.requestJson<TaskRecordView[]>('GET', '/tasks')
  }

  getTask(taskId: string): Promise<TaskRecordView> {
    return this.requestJson<TaskRecordView>('GET', `/tasks/${encodeURIComponent(taskId)}`)
  }

  pauseTask(taskId: string): Promise<TaskRecordView> {
    return this.requestJson<TaskRecordView>(
      'POST',
      `/tasks/${encodeURIComponent(taskId)}/pause`,
    )
  }

  resumeTask(taskId: string): Promise<TaskRecordView> {
    return this.requestJson<TaskRecordView>(
      'POST',
      `/tasks/${encodeURIComponent(taskId)}/resume`,
    )
  }

  cancelTask(taskId: string): Promise<TaskRecordView> {
    return this.requestJson<TaskRecordView>(
      'POST',
      `/tasks/${encodeURIComponent(taskId)}/cancel`,
    )
  }

  resolveApproval(
    taskId: string,
    approvalId: string,
    decision: ApprovalDecisionPayload,
  ): Promise<TaskRecordView> {
    return this.requestJson<TaskRecordView>(
      'POST',
      `/tasks/${encodeURIComponent(taskId)}/approvals/${encodeURIComponent(approvalId)}`,
      decision,
    )
  }

  emergencyStop(): Promise<EmergencyStopResponse> {
    return this.requestJson<EmergencyStopResponse>('POST', '/emergency-stop', undefined, 5000)
  }

  resetEmergencyStop(): Promise<EmergencyStopResetResponse> {
    return this.requestJson<EmergencyStopResetResponse>(
      'POST',
      '/emergency-stop/reset',
      undefined,
      5000,
    )
  }

  getSettings(): Promise<SettingsSnapshot> {
    return this.requestJson<SettingsSnapshot>('GET', '/settings')
  }

  updateSettings(selection: ModelSelectionState): Promise<SettingsSnapshot> {
    return this.requestJson<SettingsSnapshot>('PUT', '/settings', { selection })
  }

  async connectWebSocket(timeoutMs = 5000): Promise<void> {
    if (this.ws?.isConnected) {
      return
    }
    this.manuallyDisconnected = false
    this.clearReconnectTimer()

    const client = new LoopbackWebSocketClient({
      host: this.host,
      port: this.port,
      path: '/ws/status',
      token: this.token,
      origin: this.origin,
      connectTimeoutMs: timeoutMs,
      onMessage: (rawText) => {
        this.handleWebSocketMessage(rawText)
      },
      onClose: () => {
        this.rejectPendingPings('WebSocket connection closed.')
        this.options.onWebSocketStateChange?.(false)
        this.scheduleReconnect()
      },
      onError: (err) => {
        this.options.onError?.(err)
      },
    })

    await client.connect()
    this.ws = client
    this.reconnectAttempt = 0
    this.options.onWebSocketStateChange?.(true)
  }

  async ping(timeoutMs = 5000): Promise<BackendPongEvent> {
    if (!this.ws || !this.ws.isConnected) {
      throw new Error('Backend WebSocket is not connected.')
    }

    const requestId = crypto.randomUUID()
    return new Promise<BackendPongEvent>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pendingPings.delete(requestId)
        reject(new Error(`Backend WebSocket ping timed out after ${timeoutMs}ms.`))
      }, timeoutMs)

      this.pendingPings.set(requestId, { resolve, reject, timer })
      try {
        this.ws?.sendJson({ type: 'ping', request_id: requestId })
      } catch (err) {
        clearTimeout(timer)
        this.pendingPings.delete(requestId)
        reject(err instanceof Error ? err : new Error(String(err)))
      }
    })
  }

  disconnect(): void {
    this.manuallyDisconnected = true
    this.clearReconnectTimer()
    this.rejectPendingPings('Backend client disconnected.')
    if (this.ws) {
      this.ws.close()
      this.ws = null
    }
    this.options.onWebSocketStateChange?.(false)
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
  }

  private scheduleReconnect(): void {
    if (this.manuallyDisconnected || this.options.autoReconnect === false) {
      return
    }
    if (this.reconnectTimer !== null) {
      return
    }
    const delayMs = Math.min(1000 * 2 ** this.reconnectAttempt, 10000)
    this.reconnectAttempt += 1
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      if (this.manuallyDisconnected) return
      void this.connectWebSocket().catch(() => {
        this.scheduleReconnect()
      })
    }, delayMs)
  }

  private handleWebSocketMessage(rawText: string): void {
    let parsed: BackendEvent
    try {
      parsed = JSON.parse(rawText) as BackendEvent
    } catch {
      this.options.onError?.(new Error('Received malformed JSON from backend WebSocket.'))
      return
    }

    if (parsed.type === 'pong' && 'request_id' in parsed && parsed.request_id) {
      const pending = this.pendingPings.get(parsed.request_id)
      if (pending) {
        clearTimeout(pending.timer)
        this.pendingPings.delete(parsed.request_id)
        pending.resolve(parsed as BackendPongEvent)
      }
    }

    this.options.onEvent?.(parsed)
  }

  private rejectPendingPings(reason: string): void {
    for (const [requestId, pending] of this.pendingPings.entries()) {
      clearTimeout(pending.timer)
      this.pendingPings.delete(requestId)
      pending.reject(new Error(reason))
    }
  }
}
