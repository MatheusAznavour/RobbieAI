import {
  app,
  BrowserWindow,
  globalShortcut,
  ipcMain,
  session,
  type IpcMainInvokeEvent,
} from 'electron'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import {
  ROBBIE_IPC_CHANNELS,
  type ApprovalDecisionPayload,
  type BackendEvent,
  type BackendStatusSnapshot,
  type ChatMode,
  type ModelSelectionState,
} from '../shared/backend'
import { BackendSidecarManager } from './backend/manager'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

process.env.APP_ROOT = path.join(__dirname, '..')

export const VITE_DEV_SERVER_URL = process.env['VITE_DEV_SERVER_URL']
export const MAIN_DIST = path.join(process.env.APP_ROOT, 'dist-electron')
export const RENDERER_DIST = path.join(process.env.APP_ROOT, 'dist')

process.env.VITE_PUBLIC = VITE_DEV_SERVER_URL
  ? path.join(process.env.APP_ROOT, 'public')
  : RENDERER_DIST

let win: BrowserWindow | null = null
let isQuitting = false

const backendManager = new BackendSidecarManager({
  isPackaged: app.isPackaged,
  appRoot: process.env.APP_ROOT,
  resourcesPath: process.resourcesPath,
})

function buildContentSecurityPolicy(devServerUrl: string | undefined): string {
  if (devServerUrl) {
    const origin = new URL(devServerUrl).origin
    const wsOrigin = origin.replace(/^http/, 'ws')
    return [
      "default-src 'self'",
      `script-src 'self' 'unsafe-inline' ${origin}`,
      "style-src 'self' 'unsafe-inline'",
      "img-src 'self' data:",
      "font-src 'self'",
      `connect-src 'self' ${origin} ${wsOrigin}`,
      "object-src 'none'",
      "base-uri 'self'",
      "form-action 'none'",
      "frame-ancestors 'none'",
    ].join('; ')
  }

  return [
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'none'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'none'",
    "frame-ancestors 'none'",
  ].join('; ')
}

function assertAuthorizedIpcSender(event: IpcMainInvokeEvent): void {
  if (!win || win.isDestroyed() || event.sender.id !== win.webContents.id) {
    throw new Error('Unauthorized IPC sender.')
  }
}

function assertNonEmptyString(value: unknown, label: string, maxLen = 8000): string {
  if (typeof value !== 'string' || !value.trim() || value.length > maxLen) {
    throw new Error(`Invalid ${label}.`)
  }
  return value
}

function registerIpcHandlers(): void {
  ipcMain.handle(ROBBIE_IPC_CHANNELS.GET_BACKEND_STATUS, (event) => {
    assertAuthorizedIpcSender(event)
    return backendManager.getSnapshot()
  })

  ipcMain.handle(ROBBIE_IPC_CHANNELS.CHECK_HEALTH, async (event) => {
    assertAuthorizedIpcSender(event)
    return backendManager.checkHealth()
  })

  ipcMain.handle(ROBBIE_IPC_CHANNELS.PING_BACKEND, async (event) => {
    assertAuthorizedIpcSender(event)
    return backendManager.ping()
  })

  ipcMain.handle(ROBBIE_IPC_CHANNELS.RESTART_BACKEND, async (event) => {
    assertAuthorizedIpcSender(event)
    return backendManager.restart()
  })

  ipcMain.handle(ROBBIE_IPC_CHANNELS.LIST_CONVERSATIONS, async (event, query?: unknown) => {
    assertAuthorizedIpcSender(event)
    const q = typeof query === 'string' ? query.slice(0, 200) : ''
    return backendManager.listConversations(q)
  })

  ipcMain.handle(ROBBIE_IPC_CHANNELS.CREATE_CONVERSATION, async (event, title?: unknown) => {
    assertAuthorizedIpcSender(event)
    const cleanTitle = typeof title === 'string' && title.trim() ? title.slice(0, 64) : undefined
    return backendManager.createConversation(cleanTitle)
  })

  ipcMain.handle(ROBBIE_IPC_CHANNELS.GET_CONVERSATION, async (event, conversationId: unknown) => {
    assertAuthorizedIpcSender(event)
    return backendManager.getConversation(assertNonEmptyString(conversationId, 'conversationId', 128))
  })

  ipcMain.handle(
    ROBBIE_IPC_CHANNELS.RENAME_CONVERSATION,
    async (event, conversationId: unknown, title: unknown) => {
      assertAuthorizedIpcSender(event)
      return backendManager.renameConversation(
        assertNonEmptyString(conversationId, 'conversationId', 128),
        assertNonEmptyString(title, 'title', 64),
      )
    },
  )

  ipcMain.handle(
    ROBBIE_IPC_CHANNELS.DELETE_CONVERSATION,
    async (event, conversationId: unknown) => {
      assertAuthorizedIpcSender(event)
      return backendManager.deleteConversation(
        assertNonEmptyString(conversationId, 'conversationId', 128),
      )
    },
  )

  ipcMain.handle(
    ROBBIE_IPC_CHANNELS.SEND_MESSAGE,
    async (event, conversationId: unknown, content: unknown, mode?: unknown) => {
      assertAuthorizedIpcSender(event)
      const validMode: ChatMode =
        mode === 'chat' || mode === 'desktop' || mode === 'auto' ? mode : 'auto'
      return backendManager.sendMessage(
        assertNonEmptyString(conversationId, 'conversationId', 128),
        assertNonEmptyString(content, 'content', 8000),
        validMode,
      )
    },
  )

  ipcMain.handle(
    ROBBIE_IPC_CHANNELS.RETRY_MESSAGE,
    async (event, conversationId: unknown, messageId: unknown) => {
      assertAuthorizedIpcSender(event)
      return backendManager.retryMessage(
        assertNonEmptyString(conversationId, 'conversationId', 128),
        assertNonEmptyString(messageId, 'messageId', 128),
      )
    },
  )

  ipcMain.handle(ROBBIE_IPC_CHANNELS.LIST_TASKS, async (event) => {
    assertAuthorizedIpcSender(event)
    return backendManager.listTasks()
  })

  ipcMain.handle(ROBBIE_IPC_CHANNELS.GET_TASK, async (event, taskId: unknown) => {
    assertAuthorizedIpcSender(event)
    return backendManager.getTask(assertNonEmptyString(taskId, 'taskId', 128))
  })

  ipcMain.handle(ROBBIE_IPC_CHANNELS.PAUSE_TASK, async (event, taskId: unknown) => {
    assertAuthorizedIpcSender(event)
    return backendManager.pauseTask(assertNonEmptyString(taskId, 'taskId', 128))
  })

  ipcMain.handle(ROBBIE_IPC_CHANNELS.RESUME_TASK, async (event, taskId: unknown) => {
    assertAuthorizedIpcSender(event)
    return backendManager.resumeTask(assertNonEmptyString(taskId, 'taskId', 128))
  })

  ipcMain.handle(ROBBIE_IPC_CHANNELS.CANCEL_TASK, async (event, taskId: unknown) => {
    assertAuthorizedIpcSender(event)
    return backendManager.cancelTask(assertNonEmptyString(taskId, 'taskId', 128))
  })

  ipcMain.handle(
    ROBBIE_IPC_CHANNELS.RESOLVE_APPROVAL,
    async (event, taskId: unknown, approvalId: unknown, decision: unknown) => {
      assertAuthorizedIpcSender(event)
      if (!decision || typeof decision !== 'object') {
        throw new Error('Invalid approval decision payload.')
      }
      const d = decision as Partial<ApprovalDecisionPayload>
      if (typeof d.approved !== 'boolean') {
        throw new Error('Approval decision requires boolean `approved`.')
      }
      return backendManager.resolveApproval(
        assertNonEmptyString(taskId, 'taskId', 128),
        assertNonEmptyString(approvalId, 'approvalId', 128),
        {
          approved: d.approved,
          step_id: assertNonEmptyString(d.step_id, 'step_id', 256),
          action_digest: assertNonEmptyString(d.action_digest, 'action_digest', 64),
          reason: typeof d.reason === 'string' ? d.reason.slice(0, 500) : undefined,
        },
      )
    },
  )

  ipcMain.handle(ROBBIE_IPC_CHANNELS.EMERGENCY_STOP, async (event) => {
    assertAuthorizedIpcSender(event)
    return backendManager.emergencyStop()
  })

  ipcMain.handle(ROBBIE_IPC_CHANNELS.RESET_EMERGENCY_STOP, async (event) => {
    assertAuthorizedIpcSender(event)
    return backendManager.resetEmergencyStop()
  })

  ipcMain.handle(ROBBIE_IPC_CHANNELS.GET_SETTINGS, async (event) => {
    assertAuthorizedIpcSender(event)
    return backendManager.getSettings()
  })

  ipcMain.handle(ROBBIE_IPC_CHANNELS.UPDATE_SETTINGS, async (event, selection: unknown) => {
    assertAuthorizedIpcSender(event)
    if (!selection || typeof selection !== 'object') {
      throw new Error('Invalid settings selection payload.')
    }
    return backendManager.updateSettings(selection as ModelSelectionState)
  })
}

function forwardBackendUpdatesToWindow(): void {
  backendManager.onStatusChange((snapshot: BackendStatusSnapshot) => {
    if (win && !win.isDestroyed()) {
      win.webContents.send(ROBBIE_IPC_CHANNELS.STATUS_CHANGED, snapshot)
    }
  })

  backendManager.onEvent((event: BackendEvent) => {
    if (win && !win.isDestroyed()) {
      win.webContents.send(ROBBIE_IPC_CHANNELS.BACKEND_EVENT, event)
    }
  })
}

function configureSessionSecurity(): void {
  const cspHeader = buildContentSecurityPolicy(VITE_DEV_SERVER_URL)
  session.defaultSession.webRequest.onHeadersReceived((details, callback) => {
    callback({
      responseHeaders: {
        ...details.responseHeaders,
        'Content-Security-Policy': [cspHeader],
      },
    })
  })
}

function registerEmergencyStopShortcut(): void {
  const candidates = ['CommandOrControl+Alt+Escape', 'CommandOrControl+Shift+X']
  for (const accelerator of candidates) {
    try {
      const ok = globalShortcut.register(accelerator, () => {
        void backendManager.emergencyStop().catch((err: unknown) => {
          console.error(
            '[Robbie] Global emergency stop failed:',
            err instanceof Error ? err.message : String(err),
          )
        })
      })
      if (ok) {
        console.info(`[Robbie] Registered global Emergency Stop shortcut: ${accelerator}`)
        return
      }
    } catch {
      // Try next fallback shortcut.
    }
  }
}

function createWindow(): void {
  win = new BrowserWindow({
    title: 'Robbie',
    width: 1280,
    height: 820,
    minWidth: 900,
    minHeight: 620,
    backgroundColor: '#0f1115',
    icon: path.join(process.env.VITE_PUBLIC, 'electron-vite.svg'),
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
    },
  })

  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  win.webContents.on('will-navigate', (event, targetUrl) => {
    const allowedPrefix = VITE_DEV_SERVER_URL ?? 'file://'
    if (!targetUrl.startsWith(allowedPrefix)) {
      event.preventDefault()
    }
  })

  win.webContents.on('did-finish-load', () => {
    win?.webContents.send(
      ROBBIE_IPC_CHANNELS.STATUS_CHANGED,
      backendManager.getSnapshot(),
    )
  })

  if (VITE_DEV_SERVER_URL) {
    void win.loadURL(VITE_DEV_SERVER_URL)
  } else {
    void win.loadFile(path.join(RENDERER_DIST, 'index.html'))
  }
}

app.on('will-quit', () => {
  globalShortcut.unregisterAll()
})

app.on('before-quit', (event) => {
  if (isQuitting) {
    return
  }
  event.preventDefault()
  isQuitting = true
  void backendManager.stop().finally(() => {
    app.quit()
  })
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit()
    win = null
  }
})

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) {
    createWindow()
  }
})

void app.whenReady().then(() => {
  configureSessionSecurity()
  registerIpcHandlers()
  forwardBackendUpdatesToWindow()
  registerEmergencyStopShortcut()
  createWindow()

  void backendManager.start().catch((err: unknown) => {
    const message = err instanceof Error ? err.message : String(err)
    console.error(`[Robbie] Backend failed to start: ${message}`)
  })
})
