import crypto from 'node:crypto'
import type {
  BackendEvent,
  BackendHealthResponse,
  BackendPongEvent,
} from '../../shared/backend.ts'
import { LoopbackWebSocketClient } from './wsClient.ts'

export const TRUSTED_BACKEND_ORIGIN = 'robbie://desktop'

export interface BackendClientOptions {
  host?: '127.0.0.1'
  port: number
  token: string
  origin?: string
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

  async checkHealth(timeoutMs = 5000): Promise<BackendHealthResponse> {
    const response = await fetch(`http://${this.host}:${this.port}/health`, {
      method: 'GET',
      headers: {
        Authorization: `Bearer ${this.token}`,
        Origin: this.origin,
      },
      signal: AbortSignal.timeout(timeoutMs),
    })

    if (!response.ok) {
      throw new Error(`Backend /health check failed with HTTP ${response.status}.`)
    }

    const payload = (await response.json()) as Partial<BackendHealthResponse>
    if (payload.status !== 'ok' || payload.service !== 'robbie-backend') {
      throw new Error('Backend /health returned an unexpected response payload.')
    }

    return payload as BackendHealthResponse
  }

  async connectWebSocket(timeoutMs = 5000): Promise<void> {
    if (this.ws?.isConnected) {
      return
    }

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
      },
      onError: (err) => {
        this.options.onError?.(err)
      },
    })

    await client.connect()
    this.ws = client
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
    this.rejectPendingPings('Backend client disconnected.')
    if (this.ws) {
      this.ws.close()
      this.ws = null
    }
    this.options.onWebSocketStateChange?.(false)
  }

  private handleWebSocketMessage(rawText: string): void {
    let parsed: BackendEvent
    try {
      parsed = JSON.parse(rawText) as BackendEvent
    } catch {
      this.options.onError?.(new Error('Received malformed JSON from backend WebSocket.'))
      return
    }

    if (parsed.type === 'pong' && parsed.request_id) {
      const pending = this.pendingPings.get(parsed.request_id)
      if (pending) {
        clearTimeout(pending.timer)
        this.pendingPings.delete(parsed.request_id)
        pending.resolve(parsed)
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
