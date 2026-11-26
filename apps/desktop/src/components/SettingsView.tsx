import { useEffect, useState } from 'react'
import type {
  ModelSelectionState,
  ProviderMetadata,
  SettingsSnapshot,
} from '../../shared/backend.ts'
import { AlertIcon, CheckIcon, LockIcon, RefreshIcon } from './Icons.tsx'

export interface SettingsViewProps {
  settings: SettingsSnapshot | null
  loading: boolean
  restartingBackend: boolean
  onSave: (selection: ModelSelectionState) => Promise<void>
  onRefresh: () => Promise<void>
  onRestartBackend: () => void
}

function findProvider(
  providers: ProviderMetadata[],
  providerId: string,
): ProviderMetadata | undefined {
  return providers.find((p) => p.id === providerId)
}

export function SettingsView({
  settings,
  loading,
  restartingBackend,
  onSave,
  onRefresh,
  onRestartBackend,
}: SettingsViewProps) {
  const [form, setForm] = useState<ModelSelectionState | null>(
    settings?.selection ?? null,
  )
  const [saving, setSaving] = useState(false)
  const [saveMessage, setSaveMessage] = useState<string | null>(null)
  const [saveError, setSaveError] = useState<string | null>(null)

  useEffect(() => {
    if (settings) {
      setForm(settings.selection)
    }
  }, [settings])

  if (!settings || !form) {
    return (
      <div className="screen-scroll">
        <div className="screen-container">
          <div className="empty-state-panel">
            <h3>Loading configuration...</h3>
            <p>Waiting for authenticated settings snapshot from backend.</p>
          </div>
        </div>
      </div>
    )
  }

  const chatProviders = settings.providers.filter((p) => p.supports_chat)
  const groundingProviders = settings.providers.filter((p) => p.supports_grounding)
  const currentChatMeta = findProvider(settings.providers, form.chat_provider)
  const currentWorkerMeta = findProvider(settings.providers, form.worker_provider)
  const currentGroundingMeta = findProvider(
    settings.providers,
    form.grounding_provider,
  )

  const dirty =
    JSON.stringify(form) !== JSON.stringify(settings.selection)

  const handleSave = async () => {
    setSaving(true)
    setSaveError(null)
    setSaveMessage(null)
    try {
      await onSave(form)
      setSaveMessage('Model settings saved locally (no secrets stored).')
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="screen-scroll">
      <div className="screen-container">
        <div className="screen-header">
          <div>
            <h2 className="screen-title">Models &amp; Environment</h2>
            <p className="screen-description">
              Configure separate conversational, desktop worker VLM, and visual
              grounding models. Provider API keys remain exclusively in backend
              environment variables.
            </p>
          </div>
          <div className="screen-header-actions">
            <button
              type="button"
              className="btn btn--secondary btn--sm"
              disabled={loading}
              onClick={() => {
                void onRefresh()
              }}
            >
              <RefreshIcon size={14} />
              <span>Refresh</span>
            </button>
            <button
              type="button"
              className="btn btn--secondary btn--sm"
              disabled={restartingBackend}
              onClick={onRestartBackend}
            >
              <span>
                {restartingBackend ? 'Restarting...' : 'Restart Backend'}
              </span>
            </button>
          </div>
        </div>

        {/* Readiness Overview */}
        <div className="readiness-grid">
          <div
            className={`readiness-card ${settings.chat_ready ? 'readiness-card--ok' : 'readiness-card--warn'}`}
          >
            <div className="readiness-card-head">
              <strong>Conversational Chat</strong>
              <span
                className={`readiness-badge ${settings.chat_ready ? 'readiness-badge--ok' : 'readiness-badge--warn'}`}
              >
                {settings.chat_ready ? 'Ready' : 'Action Required'}
              </span>
            </div>
            {settings.chat_configuration_errors.length === 0 ? (
              <p className="readiness-ok-text">
                Provider credentials and model selection are ready.
              </p>
            ) : (
              <ul className="readiness-errors">
                {settings.chat_configuration_errors.map((err, idx) => (
                  <li key={idx}>{err}</li>
                ))}
              </ul>
            )}
          </div>

          <div
            className={`readiness-card ${settings.desktop_ready ? 'readiness-card--ok' : 'readiness-card--warn'}`}
          >
            <div className="readiness-card-head">
              <strong>Agent-S Desktop Execution</strong>
              <span
                className={`readiness-badge ${settings.desktop_ready ? 'readiness-badge--ok' : 'readiness-badge--warn'}`}
              >
                {settings.desktop_ready ? 'Ready' : 'Action Required'}
              </span>
            </div>
            {settings.desktop_configuration_errors.length === 0 ? (
              <p className="readiness-ok-text">
                Worker VLM and visual grounding endpoint are configured.
              </p>
            ) : (
              <ul className="readiness-errors">
                {settings.desktop_configuration_errors.map((err, idx) => (
                  <li key={idx}>{err}</li>
                ))}
              </ul>
            )}
          </div>
        </div>

        {/* Model Selection Form */}
        <section className="settings-section">
          <h3 className="settings-section-title">1. Conversational Model</h3>
          <p className="settings-section-sub">
            Used for ordinary conversation without invoking Agent-S desktop
            control.
          </p>
          <div className="form-grid">
            <label className="form-field">
              <span>Provider</span>
              <select
                value={form.chat_provider}
                onChange={(e) => {
                  const nextProvider = e.target.value
                  const meta = findProvider(settings.providers, nextProvider)
                  setForm({
                    ...form,
                    chat_provider: nextProvider,
                    chat_model:
                      meta?.suggested_models[0] ?? form.chat_model,
                  })
                }}
              >
                {chatProviders.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.label} ({p.configured ? 'Configured' : 'Not Set'})
                  </option>
                ))}
              </select>
            </label>

            <label className="form-field">
              <span>Model Name</span>
              <input
                type="text"
                list="chat-model-suggestions"
                value={form.chat_model}
                onChange={(e) =>
                  setForm({ ...form, chat_model: e.target.value })
                }
                placeholder="e.g., gpt-4o or claude-3-7-sonnet-20250219"
              />
              <datalist id="chat-model-suggestions">
                {(currentChatMeta?.suggested_models ?? []).map((m) => (
                  <option key={m} value={m} />
                ))}
              </datalist>
            </label>

            <label className="form-field form-field--wide">
              <span>Custom Base URL (Optional)</span>
              <input
                type="text"
                value={form.chat_base_url ?? ''}
                onChange={(e) =>
                  setForm({
                    ...form,
                    chat_base_url: e.target.value.trim() || null,
                  })
                }
                placeholder="Leave blank for provider default (required for vLLM / local gateways)"
              />
            </label>
          </div>
        </section>

        <section className="settings-section">
          <h3 className="settings-section-title">2. Desktop Worker VLM</h3>
          <p className="settings-section-sub">
            Vision-language planner used by Agent-S to inspect Windows
            screenshots and propose desktop actions.
          </p>
          <div className="form-grid">
            <label className="form-field">
              <span>Provider</span>
              <select
                value={form.worker_provider}
                onChange={(e) => {
                  const nextProvider = e.target.value
                  const meta = findProvider(settings.providers, nextProvider)
                  setForm({
                    ...form,
                    worker_provider: nextProvider,
                    worker_model:
                      meta?.suggested_models[0] ?? form.worker_model,
                  })
                }}
              >
                {chatProviders.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.label} ({p.configured ? 'Configured' : 'Not Set'})
                  </option>
                ))}
              </select>
            </label>

            <label className="form-field">
              <span>Model Name</span>
              <input
                type="text"
                list="worker-model-suggestions"
                value={form.worker_model}
                onChange={(e) =>
                  setForm({ ...form, worker_model: e.target.value })
                }
              />
              <datalist id="worker-model-suggestions">
                {(currentWorkerMeta?.suggested_models ?? []).map((m) => (
                  <option key={m} value={m} />
                ))}
              </datalist>
            </label>

            <label className="form-field form-field--wide">
              <span>Custom Base URL (Optional)</span>
              <input
                type="text"
                value={form.worker_base_url ?? ''}
                onChange={(e) =>
                  setForm({
                    ...form,
                    worker_base_url: e.target.value.trim() || null,
                  })
                }
                placeholder="Leave blank for provider default"
              />
            </label>
          </div>
        </section>

        <section className="settings-section">
          <h3 className="settings-section-title">3. Visual Grounding Model</h3>
          <p className="settings-section-sub">
            Dedicated grounding model (such as UI-TARS) that maps UI targets to
            screen coordinates.
          </p>
          <div className="form-grid">
            <label className="form-field">
              <span>Provider</span>
              <select
                value={form.grounding_provider}
                onChange={(e) => {
                  const nextProvider = e.target.value
                  const meta = findProvider(settings.providers, nextProvider)
                  setForm({
                    ...form,
                    grounding_provider: nextProvider,
                    grounding_model:
                      meta?.suggested_models[0] ?? form.grounding_model,
                  })
                }}
              >
                {groundingProviders.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.label} ({p.configured ? 'Configured' : 'Not Set'})
                  </option>
                ))}
              </select>
            </label>

            <label className="form-field">
              <span>Grounding Model</span>
              <input
                type="text"
                list="grounding-model-suggestions"
                value={form.grounding_model}
                onChange={(e) =>
                  setForm({ ...form, grounding_model: e.target.value })
                }
              />
              <datalist id="grounding-model-suggestions">
                {(currentGroundingMeta?.suggested_models ?? []).map((m) => (
                  <option key={m} value={m} />
                ))}
              </datalist>
            </label>

            <label className="form-field form-field--wide">
              <span>Grounding Endpoint URL</span>
              <input
                type="text"
                value={form.grounding_url}
                onChange={(e) =>
                  setForm({ ...form, grounding_url: e.target.value })
                }
                placeholder="http://localhost:8080 or https://your-endpoint.endpoints.huggingface.cloud"
              />
            </label>

            <label className="form-field">
              <span>Grounding Width (px)</span>
              <input
                type="number"
                min={320}
                max={7680}
                value={form.grounding_width}
                onChange={(e) =>
                  setForm({
                    ...form,
                    grounding_width: Number(e.target.value) || 1920,
                  })
                }
              />
            </label>

            <label className="form-field">
              <span>Grounding Height (px)</span>
              <input
                type="number"
                min={240}
                max={4320}
                value={form.grounding_height}
                onChange={(e) =>
                  setForm({
                    ...form,
                    grounding_height: Number(e.target.value) || 1080,
                  })
                }
              />
            </label>

            <label className="form-field">
              <span>Max Steps Per Task</span>
              <input
                type="number"
                min={1}
                max={50}
                value={form.max_steps}
                onChange={(e) =>
                  setForm({
                    ...form,
                    max_steps: Number(e.target.value) || 15,
                  })
                }
              />
            </label>

            <label className="form-checkbox">
              <input
                type="checkbox"
                checked={form.enable_reflection}
                onChange={(e) =>
                  setForm({ ...form, enable_reflection: e.target.checked })
                }
              />
              <span>Enable Agent-S trajectory reflection</span>
            </label>
          </div>

          {saveError ? (
            <div className="inline-error" role="alert">
              {saveError}
            </div>
          ) : null}

          {saveMessage ? (
            <div className="inline-success" role="status">
              <CheckIcon size={14} />
              <span>{saveMessage}</span>
            </div>
          ) : null}

          <div className="settings-actions">
            <button
              type="button"
              className="btn btn--primary"
              disabled={!dirty || saving}
              onClick={() => {
                void handleSave()
              }}
            >
              {saving ? 'Saving...' : 'Save Configuration'}
            </button>
            <button
              type="button"
              className="btn btn--ghost"
              disabled={!dirty || saving}
              onClick={() => {
                setForm(settings.selection)
                setSaveError(null)
                setSaveMessage(null)
              }}
            >
              Revert Changes
            </button>
          </div>
        </section>

        {/* Provider Environment Status (No Secret Exposure) */}
        <section className="settings-section">
          <h3 className="settings-section-title">
            Provider Environment Status
          </h3>
          <p className="settings-section-sub">
            API keys are read only by the local Python backend from environment
            variables or <code>.env</code> and are never exposed to Electron or
            the UI. After updating <code>.env</code>, click{' '}
            <strong>Restart Backend</strong> above.
          </p>
          <div className="provider-table">
            {settings.providers.map((provider) => (
              <div key={provider.id} className="provider-row">
                <div>
                  <div className="provider-name">{provider.label}</div>
                  <div className="provider-env mono">
                    {provider.credential_env_var ??
                      provider.endpoint_env_var ??
                      'Local endpoint'}
                  </div>
                </div>
                <span
                  className={`provider-badge ${provider.configured ? 'provider-badge--ok' : 'provider-badge--missing'}`}
                >
                  {provider.configured ? 'Configured' : 'Not Set'}
                </span>
              </div>
            ))}
          </div>
        </section>

        {/* Locked Safety Policy Notice */}
        <section className="settings-section settings-section--locked">
          <div className="locked-header">
            <LockIcon size={16} />
            <h3>Safety Guardrails (Locked)</h3>
          </div>
          <p className="settings-section-sub">
            Robbie enforces a fail-closed AST action parser, Windows LOLBin and
            shell blocking, disabled Agent-S LocalEnv/Code Agent, typed-text
            redaction, human-in-the-loop approvals, and persistent Emergency
            Stop latching. These guardrails cannot be weakened or disabled from
            the desktop interface.
          </p>
          {settings.requires_restart_on_env_change ? (
            <div className="restart-advisory">
              <AlertIcon size={14} />
              <span>
                Environment variable updates require restarting the local
                backend process to take effect.
              </span>
            </div>
          ) : null}
        </section>
      </div>
    </div>
  )
}
