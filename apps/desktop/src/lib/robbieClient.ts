import type {
  BackendEvent,
  BackendHealthResponse,
  BackendPongEvent,
  BackendStatusSnapshot,
  RobbieApi,
} from '../../shared/backend.ts'

/**
 * Typed Renderer-side facade over the narrowly scoped `window.robbie` preload bridge.
 *
 * The Renderer never receives the backend bearer token or direct Node/IPC access;
 * all authenticated HTTP and WebSocket traffic is mediated by Electron Main.
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
}

export const robbieClient = new RobbieRendererClient(() =>
  typeof window !== 'undefined' ? window.robbie : undefined,
)
