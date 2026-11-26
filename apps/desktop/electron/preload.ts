import { contextBridge, ipcRenderer, type IpcRendererEvent } from 'electron'
import {
  ROBBIE_IPC_CHANNELS,
  type BackendEvent,
  type BackendHealthResponse,
  type BackendPongEvent,
  type BackendStatusSnapshot,
  type RobbieApi,
} from '../shared/backend'

// Expose only a narrow, typed API to the Renderer.
// Never expose raw ipcRenderer, Node.js APIs, or backend bearer tokens.
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
}

contextBridge.exposeInMainWorld('robbie', robbieApi)
