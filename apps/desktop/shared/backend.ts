export type BackendLifecycleState =
  | 'stopped'
  | 'starting'
  | 'ready'
  | 'restarting'
  | 'stopping'
  | 'error'

export type ChatMode = 'auto' | 'chat' | 'desktop'

export type TaskLifecycleState =
  | 'pending'
  | 'running'
  | 'paused'
  | 'awaiting_approval'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'emergency_stopped'

export interface BackendStatusSnapshot {
  state: BackendLifecycleState
  host: string | null
  port: number | null
  pid: number | null
  version: string | null
  wsConnected: boolean
  emergencyStopped: boolean
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
  emergency_stopped: boolean
  timestamp: string
}

export interface ChatMessage {
  message_id: string
  conversation_id: string
  role: 'user' | 'assistant'
  content: string
  kind: 'chat' | 'task_started' | 'task_summary' | 'error'
  task_id: string | null
  created_at: string
}

export interface ConversationSummary {
  conversation_id: string
  title: string
  created_at: string
  updated_at: string
  message_count: number
  task_count: number
  last_message_preview: string
}

export interface PendingApproval {
  approval_id: string
  task_id: string
  step_id: string
  action_digest: string
  action: Record<string, unknown>
  risk_level: 'low' | 'medium' | 'high' | 'critical' | string
  category: string
  reasons: string[]
  created_at: string
}

export type TaskEventType =
  | 'task_started'
  | 'action_planned'
  | 'approval_required'
  | 'approval_resolved'
  | 'action_executed'
  | 'task_paused'
  | 'task_resumed'
  | 'task_completed'
  | 'task_failed'
  | 'task_cancelled'
  | 'task_emergency_stopped'

export interface TaskEventItem {
  type: TaskEventType
  timestamp: string
  task_id?: string
  conversation_id?: string
  step_id?: string
  step_index?: number
  action?: Record<string, unknown>
  risk_level?: string
  category?: string
  requires_approval?: boolean
  approval?: PendingApproval
  approval_id?: string
  approved?: boolean
  result_status?: string
  detail?: string
  reason?: string
  error?: string
  steps_executed?: number
}

export interface TaskRecordView {
  task_id: string
  conversation_id: string | null
  instruction: string
  state: TaskLifecycleState
  current_step: number
  max_steps: number
  last_step_id: string | null
  last_error: string | null
  created_at: string
  updated_at: string
  pending_approval: PendingApproval | null
  events: TaskEventItem[]
}

export interface ConversationDetail {
  conversation_id: string
  title: string
  created_at: string
  updated_at: string
  messages: ChatMessage[]
  task_ids: string[]
  tasks: TaskRecordView[]
}

export interface SendMessageResponse {
  conversation: ConversationSummary
  user_message: ChatMessage
  assistant_message: ChatMessage
  task: TaskRecordView | null
}

export interface ApprovalDecisionPayload {
  approved: boolean
  step_id: string
  action_digest: string
  reason?: string
}

export interface EmergencyStopResponse {
  status: 'emergency_stopped'
  affected_task_ids: string[]
  timestamp: string
}

export interface EmergencyStopResetResponse {
  status: 'ready'
  emergency_stopped: false
  timestamp: string
}

export interface ProviderMetadata {
  id: string
  label: string
  credential_env_var: string | null
  endpoint_env_var: string | null
  configured: boolean
  supports_chat: boolean
  supports_grounding: boolean
  suggested_models: string[]
}

export interface ModelSelectionState {
  chat_provider: string
  chat_model: string
  chat_base_url: string | null
  worker_provider: string
  worker_model: string
  worker_base_url: string | null
  grounding_provider: string
  grounding_model: string
  grounding_url: string
  grounding_width: number
  grounding_height: number
  max_steps: number
  enable_reflection: boolean
}

export interface SettingsSnapshot {
  selection: ModelSelectionState
  providers: ProviderMetadata[]
  chat_ready: boolean
  chat_configuration_errors: string[]
  desktop_ready: boolean
  desktop_configuration_errors: string[]
  safety_policy_locked: boolean
  requires_restart_on_env_change: boolean
}

export interface BackendStatusEvent {
  type: 'status'
  status: string
  version: string
  host: string
  port: number
  pid: number
  uptime_seconds: number
  emergency_stopped?: boolean
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

export interface ConversationMessageEvent {
  type: 'conversation_message'
  conversation_id: string
  message: ChatMessage
  timestamp?: string
}

export interface EmergencyStopEvent {
  type: 'emergency_stop'
  affected_task_ids: string[]
  timestamp: string
}

export interface EmergencyStopResetEvent {
  type: 'emergency_stop_reset'
  emergency_stopped: false
  timestamp: string
}

export type BackendEvent =
  | BackendStatusEvent
  | BackendPongEvent
  | BackendErrorEvent
  | BackendShutdownEvent
  | ConversationMessageEvent
  | EmergencyStopEvent
  | EmergencyStopResetEvent
  | TaskEventItem

export const ROBBIE_IPC_CHANNELS = {
  GET_BACKEND_STATUS: 'robbie:backend:get-status',
  CHECK_HEALTH: 'robbie:backend:check-health',
  PING_BACKEND: 'robbie:backend:ping',
  RESTART_BACKEND: 'robbie:backend:restart',
  STATUS_CHANGED: 'robbie:backend:status-changed',
  BACKEND_EVENT: 'robbie:backend:event',

  LIST_CONVERSATIONS: 'robbie:conversations:list',
  CREATE_CONVERSATION: 'robbie:conversations:create',
  GET_CONVERSATION: 'robbie:conversations:get',
  RENAME_CONVERSATION: 'robbie:conversations:rename',
  DELETE_CONVERSATION: 'robbie:conversations:delete',
  SEND_MESSAGE: 'robbie:conversations:send-message',
  RETRY_MESSAGE: 'robbie:conversations:retry-message',

  LIST_TASKS: 'robbie:tasks:list',
  GET_TASK: 'robbie:tasks:get',
  PAUSE_TASK: 'robbie:tasks:pause',
  RESUME_TASK: 'robbie:tasks:resume',
  CANCEL_TASK: 'robbie:tasks:cancel',
  RESOLVE_APPROVAL: 'robbie:tasks:resolve-approval',

  EMERGENCY_STOP: 'robbie:safety:emergency-stop',
  RESET_EMERGENCY_STOP: 'robbie:safety:emergency-stop-reset',

  GET_SETTINGS: 'robbie:settings:get',
  UPDATE_SETTINGS: 'robbie:settings:update',
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

  listConversations: (query?: string) => Promise<ConversationSummary[]>
  createConversation: (title?: string) => Promise<ConversationDetail>
  getConversation: (conversationId: string) => Promise<ConversationDetail>
  renameConversation: (
    conversationId: string,
    title: string,
  ) => Promise<ConversationSummary>
  deleteConversation: (conversationId: string) => Promise<void>
  sendMessage: (
    conversationId: string,
    content: string,
    mode?: ChatMode,
  ) => Promise<SendMessageResponse>
  retryMessage: (
    conversationId: string,
    messageId: string,
  ) => Promise<SendMessageResponse>

  listTasks: () => Promise<TaskRecordView[]>
  getTask: (taskId: string) => Promise<TaskRecordView>
  pauseTask: (taskId: string) => Promise<TaskRecordView>
  resumeTask: (taskId: string) => Promise<TaskRecordView>
  cancelTask: (taskId: string) => Promise<TaskRecordView>
  resolveApproval: (
    taskId: string,
    approvalId: string,
    decision: ApprovalDecisionPayload,
  ) => Promise<TaskRecordView>

  emergencyStop: () => Promise<EmergencyStopResponse>
  resetEmergencyStop: () => Promise<EmergencyStopResetResponse>

  getSettings: () => Promise<SettingsSnapshot>
  updateSettings: (selection: ModelSelectionState) => Promise<SettingsSnapshot>
}
