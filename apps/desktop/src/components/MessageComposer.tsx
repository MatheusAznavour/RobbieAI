import { useEffect, useRef, type KeyboardEvent } from 'react'
import type { ChatMode } from '../../shared/backend.ts'
import { DesktopIcon, SendIcon } from './Icons.tsx'

export interface MessageComposerProps {
  value: string
  mode: ChatMode
  disabled: boolean
  sending: boolean
  emergencyStopped: boolean
  desktopReady: boolean
  error: string | null
  onChange: (value: string) => void
  onModeChange: (mode: ChatMode) => void
  onSubmit: () => void
}

const MODES: Array<{ id: ChatMode; label: string; hint: string }> = [
  {
    id: 'auto',
    label: 'Auto',
    hint: 'Automatically distinguishes ordinary chat from Windows desktop tasks',
  },
  {
    id: 'chat',
    label: 'Chat',
    hint: 'Conversation only — never launches Agent-S desktop automation',
  },
  {
    id: 'desktop',
    label: 'Desktop',
    hint: 'Execute this instruction on the Windows desktop through Agent-S',
  },
]

export function MessageComposer({
  value,
  mode,
  disabled,
  sending,
  emergencyStopped,
  desktopReady,
  error,
  onChange,
  onModeChange,
  onSubmit,
}: MessageComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)

  useEffect(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    const nextHeight = Math.min(el.scrollHeight, 180)
    el.style.height = `${Math.max(nextHeight, 44)}px`
  }, [value])

  const canSubmit = !disabled && !sending && value.trim().length > 0

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      if (canSubmit) {
        onSubmit()
      }
    }
  }

  return (
    <div className="composer-container">
      {error ? (
        <div className="composer-error" role="alert">
          {error}
        </div>
      ) : null}

      <div className="composer-box">
        <textarea
          ref={textareaRef}
          className="composer-textarea"
          rows={1}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={handleKeyDown}
          disabled={disabled || sending}
          placeholder={
            mode === 'desktop'
              ? 'Describe a Windows desktop task for Robbie to perform...'
              : 'Message Robbie, or ask to perform a task on your Windows desktop...'
          }
          aria-label="Message Robbie"
        />

        <div className="composer-footer">
          <div className="mode-selector" role="radiogroup" aria-label="Execution mode">
            {MODES.map((item) => (
              <button
                key={item.id}
                type="button"
                role="radio"
                aria-checked={mode === item.id}
                className={`mode-chip ${mode === item.id ? 'mode-chip--active' : ''}`}
                onClick={() => onModeChange(item.id)}
                title={item.hint}
                disabled={sending}
              >
                {item.id === 'desktop' ? <DesktopIcon size={12} /> : null}
                <span>{item.label}</span>
              </button>
            ))}
          </div>

          <div className="composer-right">
            <span className="composer-hint">
              {emergencyStopped && mode === 'desktop'
                ? 'Emergency stop active — reset latch before desktop tasks'
                : !desktopReady && mode === 'desktop'
                  ? 'Desktop models incomplete — check Settings'
                  : 'Enter to send · Shift+Enter for new line'}
            </span>
            <button
              type="button"
              className="send-btn"
              disabled={!canSubmit}
              onClick={onSubmit}
              aria-label="Send message"
              title="Send message"
            >
              <SendIcon size={16} />
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
