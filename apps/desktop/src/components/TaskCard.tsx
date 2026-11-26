import { useState } from 'react'
import type {
  ApprovalDecisionPayload,
  TaskEventItem,
  TaskLifecycleState,
  TaskRecordView,
} from '../../shared/backend.ts'
import { isActiveTaskState } from '../lib/stateReconciler.ts'
import { ApprovalCard } from './ApprovalCard.tsx'
import { DesktopIcon, PauseIcon, PlayIcon, XIcon } from './Icons.tsx'

export interface TaskCardProps {
  task: TaskRecordView
  onPause: (taskId: string) => Promise<void>
  onResume: (taskId: string) => Promise<void>
  onCancel: (taskId: string) => Promise<void>
  onDecideApproval: (
    taskId: string,
    approvalId: string,
    decision: ApprovalDecisionPayload,
  ) => Promise<void>
  onRefreshAuthoritative: () => Promise<void>
  onOpenConversation?: (conversationId: string) => void
}

function stateLabel(state: TaskLifecycleState): string {
  switch (state) {
    case 'pending':
      return 'Pending'
    case 'running':
      return 'Running'
    case 'paused':
      return 'Paused'
    case 'awaiting_approval':
      return 'Awaiting Approval'
    case 'completed':
      return 'Completed'
    case 'failed':
      return 'Failed'
    case 'cancelled':
      return 'Cancelled'
    case 'emergency_stopped':
      return 'Emergency Stopped'
  }
}

function describeEvent(ev: TaskEventItem): string {
  switch (ev.type) {
    case 'task_started':
      return 'Desktop execution started'
    case 'action_planned': {
      const kind = String(ev.action?.kind ?? 'action')
      return `Planned ${kind}${ev.risk_level ? ` (${ev.risk_level} risk)` : ''}`
    }
    case 'approval_required':
      return `Approval requested (${ev.approval?.category ?? 'policy gate'})`
    case 'approval_resolved':
      return ev.approved ? 'Human approved action' : 'Human rejected action'
    case 'action_executed':
      return `Action ${ev.result_status ?? 'executed'}${ev.detail ? ` — ${ev.detail}` : ''}`
    case 'task_paused':
      return 'Task paused'
    case 'task_resumed':
      return 'Task resumed'
    case 'task_completed':
      return `Completed (${ev.steps_executed ?? 0} steps)`
    case 'task_failed':
      return `Failed${ev.error ? ` — ${ev.error}` : ''}`
    case 'task_cancelled':
      return `Cancelled${ev.reason ? ` — ${ev.reason}` : ''}`
    case 'task_emergency_stopped':
      return 'Halted by Emergency Stop'
    default:
      return ev.type
  }
}

function formatTime(iso?: string): string {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleTimeString([], {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    })
  } catch {
    return ''
  }
}

export function TaskCard({
  task,
  onPause,
  onResume,
  onCancel,
  onDecideApproval,
  onRefreshAuthoritative,
  onOpenConversation,
}: TaskCardProps) {
  const [busy, setBusy] = useState<'pause' | 'resume' | 'cancel' | null>(null)
  const [expanded, setExpanded] = useState(
    task.state === 'running' ||
      task.state === 'awaiting_approval' ||
      task.state === 'failed',
  )
  const [actionError, setActionError] = useState<string | null>(null)

  const active = isActiveTaskState(task.state)
  const recentEvents = task.events.slice(-8)

  const runControl = async (
    kind: 'pause' | 'resume' | 'cancel',
    fn: (id: string) => Promise<void>,
  ) => {
    if (busy) return
    setBusy(kind)
    setActionError(null)
    try {
      await fn(task.task_id)
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(null)
    }
  }

  return (
    <article
      className={`task-card task-card--${task.state}`}
      aria-label={`Desktop task ${task.task_id}`}
    >
      <div className="task-card-header">
        <div className="task-card-title-group">
          <span className="task-card-icon">
            <DesktopIcon size={15} />
          </span>
          <div className="task-card-heading-text">
            <div className="task-card-eyebrow">
              <span>Desktop Execution</span>
              <span className="dot-sep" aria-hidden="true">
                ·
              </span>
              <span className="mono">{task.task_id.slice(0, 8)}</span>
              {task.max_steps > 0 ? (
                <>
                  <span className="dot-sep" aria-hidden="true">
                    ·
                  </span>
                  <span>
                    Step {task.current_step} / {task.max_steps}
                  </span>
                </>
              ) : null}
            </div>
            <div className="task-card-instruction">
              {task.instruction || 'Desktop automation task'}
            </div>
          </div>
        </div>

        <div className="task-card-badges">
          <span className={`status-pill status-pill--${task.state}`}>
            {stateLabel(task.state)}
          </span>
        </div>
      </div>

      <div className="task-card-toolbar">
        <div className="task-card-actions">
          {task.state === 'running' ? (
            <button
              type="button"
              className="btn btn--ghost btn--xs"
              disabled={busy !== null}
              onClick={() => {
                void runControl('pause', onPause)
              }}
            >
              <PauseIcon size={12} />
              <span>{busy === 'pause' ? 'Pausing...' : 'Pause'}</span>
            </button>
          ) : null}

          {task.state === 'paused' ? (
            <button
              type="button"
              className="btn btn--ghost btn--xs"
              disabled={busy !== null}
              onClick={() => {
                void runControl('resume', onResume)
              }}
            >
              <PlayIcon size={12} />
              <span>{busy === 'resume' ? 'Resuming...' : 'Resume'}</span>
            </button>
          ) : null}

          {active ? (
            <button
              type="button"
              className="btn btn--danger-ghost btn--xs"
              disabled={busy !== null}
              onClick={() => {
                void runControl('cancel', onCancel)
              }}
            >
              <XIcon size={12} />
              <span>{busy === 'cancel' ? 'Cancelling...' : 'Cancel'}</span>
            </button>
          ) : null}

          {onOpenConversation && task.conversation_id ? (
            <button
              type="button"
              className="btn btn--ghost btn--xs"
              onClick={() => onOpenConversation(task.conversation_id!)}
            >
              Open Chat
            </button>
          ) : null}
        </div>

        <button
          type="button"
          className="btn btn--text btn--xs"
          onClick={() => setExpanded((prev) => !prev)}
          aria-expanded={expanded}
        >
          {expanded
            ? 'Hide timeline'
            : `Timeline (${task.events.length})`}
        </button>
      </div>

      {actionError ? (
        <div className="inline-error" role="alert">
          {actionError}
        </div>
      ) : null}

      {task.last_error ? (
        <div className="task-card-error" role="alert">
          <strong>Outcome Error:</strong> {task.last_error}
        </div>
      ) : null}

      {task.state === 'awaiting_approval' && task.pending_approval ? (
        <div className="task-card-approval">
          <ApprovalCard
            approval={task.pending_approval}
            compact
            onDecide={onDecideApproval}
            onRefreshAuthoritative={onRefreshAuthoritative}
          />
        </div>
      ) : null}

      {expanded ? (
        <div className="task-timeline">
          {recentEvents.length === 0 ? (
            <div className="task-timeline-empty">
              Waiting for execution events from backend...
            </div>
          ) : (
            <ul>
              {recentEvents.map((ev, idx) => (
                <li
                  key={`${ev.timestamp}-${ev.type}-${idx}`}
                  className="task-timeline-row"
                >
                  <span className="task-timeline-time mono">
                    {formatTime(ev.timestamp)}
                  </span>
                  <span className="task-timeline-text">
                    {describeEvent(ev)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}
    </article>
  )
}
