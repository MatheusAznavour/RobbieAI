import type { ApprovalDecisionPayload } from '../../shared/backend.ts'
import type { ApprovalWithTaskContext } from '../lib/stateReconciler.ts'
import { ApprovalCard } from './ApprovalCard.tsx'
import { LockIcon, RefreshIcon, ShieldIcon } from './Icons.tsx'

export interface ApprovalsViewProps {
  approvals: ApprovalWithTaskContext[]
  loading: boolean
  onDecideApproval: (
    taskId: string,
    approvalId: string,
    decision: ApprovalDecisionPayload,
  ) => Promise<void>
  onRefresh: () => Promise<void>
  onOpenConversation: (conversationId: string) => void
}

export function ApprovalsView({
  approvals,
  loading,
  onDecideApproval,
  onRefresh,
  onOpenConversation,
}: ApprovalsViewProps) {
  return (
    <div className="screen-scroll">
      <div className="screen-container">
        <div className="screen-header">
          <div>
            <h2 className="screen-title">Human Approval Center</h2>
            <p className="screen-description">
              Desktop actions flagged by Robbie&apos;s fail-closed safety policy
              pause here for explicit review before execution.
            </p>
          </div>
          <button
            type="button"
            className="btn btn--secondary btn--sm"
            disabled={loading}
            onClick={() => {
              void onRefresh()
            }}
          >
            <RefreshIcon size={14} />
            <span>{loading ? 'Refreshing...' : 'Refresh'}</span>
          </button>
        </div>

        <div className="policy-info-banner">
          <LockIcon size={15} />
          <div>
            <strong>Non-bypassable safety boundary:</strong> Shell interpreters,
            Windows LOLBins, command-line switches, URLs, scripts, and
            arbitrary code execution are hard-blocked by the backend AST parser
            and safety policy and can never be approved from the UI. Typed text
            payloads are always redacted to <code>[REDACTED]</code>.
          </div>
        </div>

        {approvals.length === 0 ? (
          <div className="empty-state-panel">
            <div className="empty-state-icon">
              <ShieldIcon size={28} />
            </div>
            <h3>No pending approvals</h3>
            <p>
              When a Windows desktop task requests a sensitive, high-risk, or
              non-allowlisted action, it will pause and appear both here and
              inline in your conversation.
            </p>
          </div>
        ) : (
          <div className="approval-stack">
            {approvals.map(({ approval, task }) => (
              <div key={approval.approval_id} className="approval-stack-item">
                {task.conversation_id ? (
                  <div className="approval-stack-bar">
                    <span className="mono">
                      Task {task.task_id.slice(0, 8)} · Step {task.current_step}{' '}
                      / {task.max_steps || '?'}
                    </span>
                    <button
                      type="button"
                      className="btn btn--ghost btn--xs"
                      onClick={() => onOpenConversation(task.conversation_id!)}
                    >
                      Open Conversation
                    </button>
                  </div>
                ) : null}
                <ApprovalCard
                  approval={approval}
                  taskInstruction={task.instruction}
                  onDecide={onDecideApproval}
                  onRefreshAuthoritative={onRefresh}
                />
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
