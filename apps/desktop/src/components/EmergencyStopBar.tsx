import type { BackendStatusSnapshot } from '../../shared/backend.ts'
import { AlertIcon, RefreshIcon, ShieldIcon, StopOctagonIcon } from './Icons.tsx'

export interface EmergencyStopBarProps {
  title: string
  subtitle?: string
  backendStatus: BackendStatusSnapshot
  pendingApprovalCount: number
  activeTaskCount: number
  emergencyPending: boolean
  resetPending: boolean
  emergencyFeedback: string | null
  onEmergencyStop: () => void
  onResetEmergencyStop: () => void
  onOpenApprovals: () => void
  onOpenTasks: () => void
}

export function EmergencyStopBar({
  title,
  subtitle,
  backendStatus,
  pendingApprovalCount,
  activeTaskCount,
  emergencyPending,
  resetPending,
  emergencyFeedback,
  onEmergencyStop,
  onResetEmergencyStop,
  onOpenApprovals,
  onOpenTasks,
}: EmergencyStopBarProps) {
  const isReady = backendStatus.state === 'ready'
  const isStopped = backendStatus.emergencyStopped

  return (
    <header className="topbar-shell">
      <div className="topbar-main">
        <div className="topbar-heading">
          <h1 className="topbar-title">{title}</h1>
          {subtitle ? <span className="topbar-subtitle">{subtitle}</span> : null}
        </div>

        <div className="topbar-controls">
          {pendingApprovalCount > 0 ? (
            <button
              type="button"
              className="topbar-pill topbar-pill--approval"
              onClick={onOpenApprovals}
              title="Review pending desktop action approvals"
            >
              <ShieldIcon size={14} />
              <span>
                {pendingApprovalCount} approval
                {pendingApprovalCount === 1 ? '' : 's'}
              </span>
            </button>
          ) : null}

          {activeTaskCount > 0 ? (
            <button
              type="button"
              className="topbar-pill topbar-pill--running"
              onClick={onOpenTasks}
              title="View active desktop tasks"
            >
              <span className="pulse-dot" aria-hidden="true" />
              <span>
                {activeTaskCount} active task{activeTaskCount === 1 ? '' : 's'}
              </span>
            </button>
          ) : null}

          <button
            type="button"
            className={
              isStopped
                ? 'emergency-btn emergency-btn--latched'
                : 'emergency-btn'
            }
            disabled={!isReady || emergencyPending}
            onClick={onEmergencyStop}
            title="Immediately halt all desktop execution (Global shortcut: Ctrl+Alt+Esc)"
            aria-label="Emergency Stop all desktop automation"
          >
            <StopOctagonIcon size={15} />
            <span>
              {emergencyPending
                ? 'Stopping...'
                : isStopped
                  ? 'Emergency Stopped'
                  : 'Emergency Stop'}
            </span>
            <kbd className="emergency-kbd">Ctrl+Alt+Esc</kbd>
          </button>
        </div>
      </div>

      {isStopped ? (
        <div
          className="emergency-latch-banner"
          role="alert"
          aria-live="assertive"
        >
          <div className="emergency-latch-info">
            <AlertIcon size={16} />
            <div>
              <strong>Emergency Stop Latch Active.</strong> All desktop tasks
              were halted and new desktop tasks are blocked until you explicitly
              reset the latch. Resetting unlocks future tasks and{' '}
              <em>does not</em> resume previously stopped tasks.
              {emergencyFeedback ? ` (${emergencyFeedback})` : ''}
            </div>
          </div>
          <button
            type="button"
            className="btn btn--secondary btn--sm"
            disabled={!isReady || resetPending}
            onClick={onResetEmergencyStop}
          >
            <RefreshIcon size={13} />
            <span>{resetPending ? 'Resetting...' : 'Reset Latch'}</span>
          </button>
        </div>
      ) : emergencyFeedback ? (
        <div className="emergency-feedback-strip" role="status">
          {emergencyFeedback}
        </div>
      ) : null}
    </header>
  )
}
