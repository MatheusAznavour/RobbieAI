import { useState } from 'react'
import type {
  ApprovalDecisionPayload,
  PendingApproval,
} from '../../shared/backend.ts'
import { isConflictError } from '../lib/stateReconciler.ts'
import { AlertIcon, CheckIcon, ShieldIcon, XIcon } from './Icons.tsx'

export interface ApprovalCardProps {
  approval: PendingApproval
  taskInstruction?: string
  compact?: boolean
  onDecide: (
    taskId: string,
    approvalId: string,
    decision: ApprovalDecisionPayload,
  ) => Promise<void>
  onRefreshAuthoritative?: () => Promise<void>
}

function formatActionSummary(action: Record<string, unknown>): Array<[string, string]> {
  const entries: Array<[string, string]> = []
  for (const [key, value] of Object.entries(action)) {
    if (value === null || value === undefined) continue
    if (Array.isArray(value)) {
      entries.push([key, value.map((v) => String(v)).join(' + ')])
    } else if (typeof value === 'object') {
      entries.push([key, JSON.stringify(value)])
    } else {
      entries.push([key, String(value)])
    }
  }
  return entries
}

function riskBadgeClass(risk: string): string {
  switch (risk.toLowerCase()) {
    case 'critical':
      return 'risk-badge risk-badge--critical'
    case 'high':
      return 'risk-badge risk-badge--high'
    case 'medium':
      return 'risk-badge risk-badge--medium'
    default:
      return 'risk-badge risk-badge--low'
  }
}

export function ApprovalCard({
  approval,
  taskInstruction,
  compact = false,
  onDecide,
  onRefreshAuthoritative,
}: ApprovalCardProps) {
  const [submitting, setSubmitting] = useState<'approve' | 'reject' | null>(null)
  const [reason, setReason] = useState('')
  const [error, setError] = useState<string | null>(null)

  const actionKind = String(approval.action.kind ?? 'desktop_action')
  const actionEntries = formatActionSummary(approval.action).filter(
    ([k]) => k !== 'kind',
  )
  const isHighRisk =
    approval.risk_level === 'high' || approval.risk_level === 'critical'

  const submitDecision = async (approved: boolean) => {
    if (submitting) return
    setSubmitting(approved ? 'approve' : 'reject')
    setError(null)
    try {
      await onDecide(approval.task_id, approval.approval_id, {
        approved,
        step_id: approval.step_id,
        action_digest: approval.action_digest,
        reason: reason.trim() || undefined,
      })
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      if (isConflictError(err)) {
        setError(
          'This approval is no longer active (already resolved, expired, or task stopped). Refreshing authoritative task state.',
        )
        if (onRefreshAuthoritative) {
          void onRefreshAuthoritative()
        }
      } else {
        setError(msg)
      }
    } finally {
      setSubmitting(null)
    }
  }

  return (
    <section
      className={`approval-card ${isHighRisk ? 'approval-card--danger' : ''} ${compact ? 'approval-card--compact' : ''}`}
      aria-label={`Human approval required for ${actionKind}`}
    >
      <div className="approval-card-header">
        <div className="approval-card-title-row">
          <span className="approval-card-icon">
            {isHighRisk ? <AlertIcon size={16} /> : <ShieldIcon size={16} />}
          </span>
          <div>
            <div className="approval-card-eyebrow">Human Approval Required</div>
            <h3 className="approval-card-title">
              <code>{actionKind}</code>
            </h3>
          </div>
        </div>
        <span className={riskBadgeClass(approval.risk_level)}>
          {approval.risk_level.toUpperCase()} RISK
        </span>
      </div>

      {taskInstruction ? (
        <div className="approval-task-context">
          <span className="meta-label">Task</span>
          <span>{taskInstruction}</span>
        </div>
      ) : null}

      <div className="approval-meta-grid">
        <div>
          <span className="meta-label">Safety Category</span>
          <span className="meta-value">{approval.category}</span>
        </div>
        <div>
          <span className="meta-label">Task / Step</span>
          <span className="meta-value mono">
            {approval.task_id.slice(0, 8)} / {approval.step_id}
          </span>
        </div>
      </div>

      {approval.reasons.length > 0 ? (
        <div className="approval-reasons">
          <div className="meta-label">Policy Reasons</div>
          <ul>
            {approval.reasons.map((item, idx) => (
              <li key={idx}>{item}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {actionEntries.length > 0 ? (
        <div className="approval-action-details">
          <div className="meta-label">Sanitized Action Payload</div>
          <dl className="kv-list">
            {actionEntries.map(([key, value]) => (
              <div key={key} className="kv-item">
                <dt>{key}</dt>
                <dd className="mono">{value}</dd>
              </div>
            ))}
          </dl>
        </div>
      ) : null}

      {error ? (
        <div className="inline-error" role="alert">
          {error}
        </div>
      ) : null}

      <div className="approval-controls">
        <input
          type="text"
          className="approval-note-input"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder="Optional audit note (e.g., Verified target window)"
          disabled={submitting !== null}
          aria-label="Optional approval note"
        />
        <div className="approval-buttons">
          <button
            type="button"
            className="btn btn--danger-outline"
            disabled={submitting !== null}
            onClick={() => {
              void submitDecision(false)
            }}
          >
            <XIcon size={14} />
            <span>{submitting === 'reject' ? 'Rejecting...' : 'Reject'}</span>
          </button>
          <button
            type="button"
            className={isHighRisk ? 'btn btn--warning' : 'btn btn--primary'}
            disabled={submitting !== null}
            onClick={() => {
              void submitDecision(true)
            }}
          >
            <CheckIcon size={14} />
            <span>
              {submitting === 'approve' ? 'Approving...' : 'Approve Action'}
            </span>
          </button>
        </div>
      </div>
    </section>
  )
}
