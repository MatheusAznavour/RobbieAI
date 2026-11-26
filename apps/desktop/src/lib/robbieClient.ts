import type {
  ApprovalDecisionPayload,
  BackendEvent,
  BackendHealthResponse,
  BackendPongEvent,
  BackendStatusSnapshot,
  ChatMode,
  ConversationDetail,
  ConversationSummary,
  EmergencyStopResetResponse,
  EmergencyStopResponse,
  ModelSelectionState,
  RobbieApi,
  SendMessageResponse,
  SettingsSnapshot,
  TaskRecordView,
} from '../../shared/backend.ts'

/**
 * Typed Renderer-side facade over the narrowly scoped `window.robbie` preload bridge.
 *
 * The Renderer never receives the backend bearer token, provider API keys, or direct
 * Node/IPC access; all authenticated HTTP and WebSocket traffic is mediated by Electron Main.
 */
export class RobbieRendererClient {
  private readonly bridgeProvider: () => RobbieApi | undefined

  constructor(bridgeProvider: () => RobbieApi | undefined) {
    this.bridgeProvider = bridgeProvider
  }

  private requireBridge(): RobbieApi {
    const api = this.bridgeProvider()
    if (!api) {
      throw new Error(
        'Robbie preload API (`window.robbie`) is unavailable in this environment.',
      )
    }
    return api
  }

  getBackendStatus(): Promise<BackendStatusSnapshot> {
    return this.requireBridge().getBackendStatus()
  }

  checkHealth(): Promise<BackendHealthResponse> {
    return this.requireBridge().checkHealth()
  }

  pingBackend(): Promise<BackendPongEvent> {
    return this.requireBridge().pingBackend()
  }

  restartBackend(): Promise<BackendStatusSnapshot> {
    return this.requireBridge().restartBackend()
  }

  onBackendStatusChange(
    listener: (snapshot: BackendStatusSnapshot) => void,
  ): () => void {
    return this.requireBridge().onBackendStatusChange(listener)
  }

  onBackendEvent(listener: (event: BackendEvent) => void): () => void {
    return this.requireBridge().onBackendEvent(listener)
  }

  listConversations(query?: string): Promise<ConversationSummary[]> {
    return this.requireBridge().listConversations(query)
  }

  createConversation(title?: string): Promise<ConversationDetail> {
    return this.requireBridge().createConversation(title)
  }

  getConversation(conversationId: string): Promise<ConversationDetail> {
    return this.requireBridge().getConversation(conversationId)
  }

  renameConversation(
    conversationId: string,
    title: string,
  ): Promise<ConversationSummary> {
    return this.requireBridge().renameConversation(conversationId, title)
  }

  deleteConversation(conversationId: string): Promise<void> {
    return this.requireBridge().deleteConversation(conversationId)
  }

  sendMessage(
    conversationId: string,
    content: string,
    mode: ChatMode = 'auto',
  ): Promise<SendMessageResponse> {
    return this.requireBridge().sendMessage(conversationId, content, mode)
  }

  retryMessage(
    conversationId: string,
    messageId: string,
  ): Promise<SendMessageResponse> {
    return this.requireBridge().retryMessage(conversationId, messageId)
  }

  listTasks(): Promise<TaskRecordView[]> {
    return this.requireBridge().listTasks()
  }

  getTask(taskId: string): Promise<TaskRecordView> {
    return this.requireBridge().getTask(taskId)
  }

  pauseTask(taskId: string): Promise<TaskRecordView> {
    return this.requireBridge().pauseTask(taskId)
  }

  resumeTask(taskId: string): Promise<TaskRecordView> {
    return this.requireBridge().resumeTask(taskId)
  }

  cancelTask(taskId: string): Promise<TaskRecordView> {
    return this.requireBridge().cancelTask(taskId)
  }

  resolveApproval(
    taskId: string,
    approvalId: string,
    decision: ApprovalDecisionPayload,
  ): Promise<TaskRecordView> {
    return this.requireBridge().resolveApproval(taskId, approvalId, decision)
  }

  emergencyStop(): Promise<EmergencyStopResponse> {
    return this.requireBridge().emergencyStop()
  }

  resetEmergencyStop(): Promise<EmergencyStopResetResponse> {
    return this.requireBridge().resetEmergencyStop()
  }

  getSettings(): Promise<SettingsSnapshot> {
    return this.requireBridge().getSettings()
  }

  updateSettings(selection: ModelSelectionState): Promise<SettingsSnapshot> {
    return this.requireBridge().updateSettings(selection)
  }
}

export const robbieClient = new RobbieRendererClient(() =>
  typeof window !== 'undefined' ? window.robbie : undefined,
)
