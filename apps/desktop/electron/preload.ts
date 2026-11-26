import { contextBridge, ipcRenderer, type IpcRendererEvent } from 'electron'
import {
  ROBBIE_IPC_CHANNELS,
  type ApprovalDecisionPayload,
  type BackendEvent,
  type BackendHealthResponse,
  type BackendPongEvent,
  type BackendStatusSnapshot,
  type ChatMode,
  type ConversationDetail,
  type ConversationSummary,
  type EmergencyStopResetResponse,
  type EmergencyStopResponse,
  type ModelSelectionState,
  type RobbieApi,
  type SendMessageResponse,
  type SettingsSnapshot,
  type TaskRecordView,
} from '../shared/backend'

// Expose only a narrow, typed API to the Renderer.
// Never expose raw ipcRenderer, Node.js APIs, backend bearer tokens, or provider API keys.
const robbieApi: RobbieApi = {
  getBackendStatus: (): Promise<BackendStatusSnapshot> =>
    ipcRenderer.invoke(ROBBIE_IPC_CHANNELS.GET_BACKEND_STATUS),

  checkHealth: (): Promise<BackendHealthResponse> =>
    ipcRenderer.invoke(ROBBIE_IPC_CHANNELS.CHECK_HEALTH),

  pingBackend: (): Promise<BackendPongEvent> =>
    ipcRenderer.invoke(ROBBIE_IPC_CHANNELS.PING_BACKEND),

  restartBackend: (): Promise<BackendStatusSnapshot> =>
    ipcRenderer.invoke(ROBBIE_IPC_CHANNELS.RESTART_BACKEND),

  onBackendStatusChange: (
    listener: (snapshot: BackendStatusSnapshot) => void,
  ): (() => void) => {
    const wrapped = (_event: IpcRendererEvent, snapshot: BackendStatusSnapshot) => {
      listener(snapshot)
    }
    ipcRenderer.on(ROBBIE_IPC_CHANNELS.STATUS_CHANGED, wrapped)
    return () => {
      ipcRenderer.off(ROBBIE_IPC_CHANNELS.STATUS_CHANGED, wrapped)
    }
  },

  onBackendEvent: (listener: (event: BackendEvent) => void): (() => void) => {
    const wrapped = (_event: IpcRendererEvent, payload: BackendEvent) => {
      listener(payload)
    }
    ipcRenderer.on(ROBBIE_IPC_CHANNELS.BACKEND_EVENT, wrapped)
    return () => {
      ipcRenderer.off(ROBBIE_IPC_CHANNELS.BACKEND_EVENT, wrapped)
    }
  },

  listConversations: (query?: string): Promise<ConversationSummary[]> =>
    ipcRenderer.invoke(ROBBIE_IPC_CHANNELS.LIST_CONVERSATIONS, query ?? ''),

  createConversation: (title?: string): Promise<ConversationDetail> =>
    ipcRenderer.invoke(ROBBIE_IPC_CHANNELS.CREATE_CONVERSATION, title),

  getConversation: (conversationId: string): Promise<ConversationDetail> =>
    ipcRenderer.invoke(ROBBIE_IPC_CHANNELS.GET_CONVERSATION, conversationId),

  renameConversation: (
    conversationId: string,
    title: string,
  ): Promise<ConversationSummary> =>
    ipcRenderer.invoke(ROBBIE_IPC_CHANNELS.RENAME_CONVERSATION, conversationId, title),

  deleteConversation: (conversationId: string): Promise<void> =>
    ipcRenderer.invoke(ROBBIE_IPC_CHANNELS.DELETE_CONVERSATION, conversationId),

  sendMessage: (
    conversationId: string,
    content: string,
    mode: ChatMode = 'auto',
  ): Promise<SendMessageResponse> =>
    ipcRenderer.invoke(ROBBIE_IPC_CHANNELS.SEND_MESSAGE, conversationId, content, mode),

  retryMessage: (
    conversationId: string,
    messageId: string,
  ): Promise<SendMessageResponse> =>
    ipcRenderer.invoke(ROBBIE_IPC_CHANNELS.RETRY_MESSAGE, conversationId, messageId),

  listTasks: (): Promise<TaskRecordView[]> =>
    ipcRenderer.invoke(ROBBIE_IPC_CHANNELS.LIST_TASKS),

  getTask: (taskId: string): Promise<TaskRecordView> =>
    ipcRenderer.invoke(ROBBIE_IPC_CHANNELS.GET_TASK, taskId),

  pauseTask: (taskId: string): Promise<TaskRecordView> =>
    ipcRenderer.invoke(ROBBIE_IPC_CHANNELS.PAUSE_TASK, taskId),

  resumeTask: (taskId: string): Promise<TaskRecordView> =>
    ipcRenderer.invoke(ROBBIE_IPC_CHANNELS.RESUME_TASK, taskId),

  cancelTask: (taskId: string): Promise<TaskRecordView> =>
    ipcRenderer.invoke(ROBBIE_IPC_CHANNELS.CANCEL_TASK, taskId),

  resolveApproval: (
    taskId: string,
    approvalId: string,
    decision: ApprovalDecisionPayload,
  ): Promise<TaskRecordView> =>
    ipcRenderer.invoke(ROBBIE_IPC_CHANNELS.RESOLVE_APPROVAL, taskId, approvalId, decision),

  emergencyStop: (): Promise<EmergencyStopResponse> =>
    ipcRenderer.invoke(ROBBIE_IPC_CHANNELS.EMERGENCY_STOP),

  resetEmergencyStop: (): Promise<EmergencyStopResetResponse> =>
    ipcRenderer.invoke(ROBBIE_IPC_CHANNELS.RESET_EMERGENCY_STOP),

  getSettings: (): Promise<SettingsSnapshot> =>
    ipcRenderer.invoke(ROBBIE_IPC_CHANNELS.GET_SETTINGS),

  updateSettings: (selection: ModelSelectionState): Promise<SettingsSnapshot> =>
    ipcRenderer.invoke(ROBBIE_IPC_CHANNELS.UPDATE_SETTINGS, selection),
}

contextBridge.exposeInMainWorld('robbie', robbieApi)
