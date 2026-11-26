export type BackendLifecycleState =
  | 'stopped'
  | 'starting'
  | 'ready'
  | 'restarting'
  | 'stopping'
  | 'error'

export interface BackendStatusSnapshot {
  state: BackendLifecycleState
  host: string | null
  port: number | null
  pid: number | null
  version: string | null
  wsConnected: boolean
  lastError: string | null
  updatedAt: string
}

export interface BackendHealthResponse {
  status: 'ok'
  service: 'robbie-backend'
  version: string
  host: string
  port: number
  pid: number
  uptime_seconds: number
  websocket_connections: number
  timestamp: string
}

export interface BackendStatusEvent {
  type: 'status'
  status: string
  version: string
  host: string
  port: number
  pid: number
  uptime_seconds: number
  timestamp: string
}

export interface BackendPongEvent {
  type: 'pong'
  request_id: string | null
  timestamp: string
}

export interface BackendErrorEvent {
  type: 'error'
  code: string
  detail: string
  timestamp: string
}

export interface BackendShutdownEvent {
  type: 'shutdown'
  timestamp: string
}

export type BackendEvent =
  | BackendStatusEvent
  | BackendPongEvent
  | BackendErrorEvent
  | BackendShutdownEvent

export const ROBBIE_IPC_CHANNELS = {
  GET_BACKEND_STATUS: 'robbie:backend:get-status',
  CHECK_HEALTH: 'robbie:backend:check-health',
  PING_BACKEND: 'robbie:backend:ping',
  RESTART_BACKEND: 'robbie:backend:restart',
  STATUS_CHANGED: 'robbie:backend:status-changed',
  BACKEND_EVENT: 'robbie:backend:event',
} as const

export interface RobbieApi {
  getBackendStatus: () => Promise<BackendStatusSnapshot>
  checkHealth: () => Promise<BackendHealthResponse>
  pingBackend: () => Promise<BackendPongEvent>
  restartBackend: () => Promise<BackendStatusSnapshot>
  onBackendStatusChange: (
    listener: (snapshot: BackendStatusSnapshot) => void,
  ) => () => void
  onBackendEvent: (listener: (event: BackendEvent) => void) => () => void
}
