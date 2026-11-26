import { app, BrowserWindow, ipcMain, session, type IpcMainInvokeEvent } from 'electron'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import {
  ROBBIE_IPC_CHANNELS,
  type BackendEvent,
  type BackendStatusSnapshot,
} from '../shared/backend'
import { BackendSidecarManager } from './backend/manager'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

// Built directory structure:
// ├─┬ dist/
// │ └── index.html
// └─┬ dist-electron/
//   ├── main.js
//   └── preload.cjs
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

function createWindow(): void {
  win = new BrowserWindow({
    title: 'Robbie',
    width: 960,
    height: 680,
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

  // Prevent navigation outside the trusted application origin and block popups.
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
  createWindow()

  void backendManager.start().catch((err: unknown) => {
    const message = err instanceof Error ? err.message : String(err)
    console.error(`[Robbie] Backend failed to start: ${message}`)
  })
})
