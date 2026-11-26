import { useEffect, useState } from 'react'
import type {
  BackendEvent,
  BackendHealthResponse,
  BackendStatusSnapshot,
} from '../shared/backend'
import { robbieClient } from './lib/robbieClient'
import './App.css'

const INITIAL_STATUS: BackendStatusSnapshot = {
  state: 'starting',
  host: null,
  port: null,
  pid: null,
  version: null,
  wsConnected: false,
  lastError: null,
  updatedAt: '',
}

function App() {
  const [status, setStatus] = useState<BackendStatusSnapshot>(INITIAL_STATUS)
  const [lastHealth, setLastHealth] = useState<BackendHealthResponse | null>(null)
  const [lastEvent, setLastEvent] = useState<BackendEvent | null>(null)
  const [actionMessage, setActionMessage] = useState<string | null>(null)

  useEffect(() => {
    let mounted = true
    let unsubscribeStatus: (() => void) | undefined
    let unsubscribeEvent: (() => void) | undefined

    try {
      unsubscribeStatus = robbieClient.onBackendStatusChange((snapshot) => {
        if (mounted) {
          setStatus(snapshot)
        }
      })

      unsubscribeEvent = robbieClient.onBackendEvent((event) => {
        if (mounted) {
          setLastEvent(event)
        }
      })

      void robbieClient
        .getBackendStatus()
        .then((snapshot) => {
          if (mounted) {
            setStatus(snapshot)
          }
        })
        .catch((err: unknown) => {
          if (mounted) {
            setActionMessage(err instanceof Error ? err.message : String(err))
          }
        })
    } catch (err: unknown) {
      setActionMessage(err instanceof Error ? err.message : String(err))
    }

    return () => {
      mounted = false
      unsubscribeStatus?.()
      unsubscribeEvent?.()
    }
  }, [])

  const handleCheckHealth = async () => {
    setActionMessage(null)
    try {
      const health = await robbieClient.checkHealth()
      setLastHealth(health)
      setActionMessage(`Health OK (uptime ${health.uptime_seconds}s)`)
    } catch (err: unknown) {
      setActionMessage(err instanceof Error ? err.message : String(err))
    }
  }

  const handlePing = async () => {
    setActionMessage(null)
    try {
      const pong = await robbieClient.pingBackend()
      setActionMessage(`WebSocket pong received at ${pong.timestamp}`)
    } catch (err: unknown) {
      setActionMessage(err instanceof Error ? err.message : String(err))
    }
  }

  const handleRestart = async () => {
    setActionMessage('Restarting backend...')
    try {
      const snapshot = await robbieClient.restartBackend()
      setStatus(snapshot)
      setActionMessage('Backend restarted.')
    } catch (err: unknown) {
      setActionMessage(err instanceof Error ? err.message : String(err))
    }
  }

  const endpoint =
    status.host && status.port ? `${status.host}:${status.port}` : 'Unavailable'

  return (
    <main>
      <h1>Robbie</h1>
      <p>Windows desktop AI agent — Milestone 1 sidecar foundation.</p>

      <section className="card" aria-label="Backend connection status">
        <h2>Backend Status</h2>
        <p>
          <strong>State:</strong> {status.state}
        </p>
        <p>
          <strong>Endpoint:</strong> {endpoint}
        </p>
        <p>
          <strong>WebSocket:</strong> {status.wsConnected ? 'connected' : 'disconnected'}
        </p>
        {status.pid !== null && (
          <p>
            <strong>PID:</strong> {status.pid}
            {status.version ? ` · v${status.version}` : ''}
          </p>
        )}
        {status.lastError && (
          <p role="alert">
            <strong>Error:</strong> {status.lastError}
          </p>
        )}

        <div>
          <button
            type="button"
            onClick={() => void handleCheckHealth()}
            disabled={status.state !== 'ready'}
          >
            Check Health
          </button>{' '}
          <button
            type="button"
            onClick={() => void handlePing()}
            disabled={status.state !== 'ready' || !status.wsConnected}
          >
            Ping WebSocket
          </button>{' '}
          <button type="button" onClick={() => void handleRestart()}>
            Restart Backend
          </button>
        </div>

        {actionMessage && <p>{actionMessage}</p>}
        {lastHealth && (
          <p className="read-the-docs">
            Last health check: {lastHealth.timestamp} (WS clients:{' '}
            {lastHealth.websocket_connections})
          </p>
        )}
        {lastEvent && (
          <p className="read-the-docs">
            Last backend event: {lastEvent.type} ({lastEvent.timestamp})
          </p>
        )}
      </section>
    </main>
  )
}

export default App
