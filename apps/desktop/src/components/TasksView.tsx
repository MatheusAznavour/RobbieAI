import { useMemo, useState } from 'react'
import type {
  ApprovalDecisionPayload,
  TaskRecordView,
} from '../../shared/backend.ts'
import { isActiveTaskState } from '../lib/stateReconciler.ts'
import { DesktopIcon, RefreshIcon } from './Icons.tsx'
import { TaskCard } from './TaskCard.tsx'

export interface TasksViewProps {
  tasks: TaskRecordView[]
  loading: boolean
  emergencyStopped: boolean
  onRefresh: () => Promise<void>
  onPauseTask: (taskId: string) => Promise<void>
  onResumeTask: (taskId: string) => Promise<void>
  onCancelTask: (taskId: string) => Promise<void>
  onDecideApproval: (
    taskId: string,
    approvalId: string,
    decision: ApprovalDecisionPayload,
  ) => Promise<void>
  onOpenConversation: (conversationId: string) => void
}

type TaskFilter = 'all' | 'active' | 'approval' | 'finished'

export function TasksView({
  tasks,
  loading,
  emergencyStopped,
  onRefresh,
  onPauseTask,
  onResumeTask,
  onCancelTask,
  onDecideApproval,
  onOpenConversation,
}: TasksViewProps) {
  const [filter, setFilter] = useState<TaskFilter>('all')

  const counts = useMemo(() => {
    let active = 0
    let approval = 0
    let finished = 0
    for (const t of tasks) {
      if (t.state === 'awaiting_approval') approval += 1
      if (isActiveTaskState(t.state)) active += 1
      else finished += 1
    }
    return { all: tasks.length, active, approval, finished }
  }, [tasks])

  const filtered = useMemo(() => {
    switch (filter) {
      case 'active':
        return tasks.filter((t) => isActiveTaskState(t.state))
      case 'approval':
        return tasks.filter((t) => t.state === 'awaiting_approval')
      case 'finished':
        return tasks.filter((t) => !isActiveTaskState(t.state))
      default:
        return tasks
    }
  }, [tasks, filter])

  return (
    <div className="screen-scroll">
      <div className="screen-container">
        <div className="screen-header">
          <div>
            <h2 className="screen-title">Desktop Tasks</h2>
            <p className="screen-description">
              Real-time execution state, safe action timelines, and lifecycle
              controls for Agent-S Windows desktop tasks.
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

        <div className="filter-bar" role="tablist" aria-label="Filter tasks">
          {(
            [
              ['all', `All (${counts.all})`],
              ['active', `Active (${counts.active})`],
              ['approval', `Awaiting Approval (${counts.approval})`],
              ['finished', `Finished (${counts.finished})`],
            ] as Array<[TaskFilter, string]>
          ).map(([id, label]) => (
            <button
              key={id}
              type="button"
              role="tab"
              aria-selected={filter === id}
              className={`filter-pill ${filter === id ? 'filter-pill--active' : ''}`}
              onClick={() => setFilter(id)}
            >
              {label}
            </button>
          ))}
        </div>

        {emergencyStopped ? (
          <div className="policy-info-banner policy-info-banner--danger">
            Emergency Stop Latch is active. New desktop tasks cannot start until
            you reset the latch in the top bar.
          </div>
        ) : null}

        {filtered.length === 0 ? (
          <div className="empty-state-panel">
            <div className="empty-state-icon">
              <DesktopIcon size={28} />
            </div>
            <h3>No tasks in this view</h3>
            <p>
              Ask Robbie to perform a Windows desktop action in Chat (or switch
              the composer mode to Desktop) to start a supervised task.
            </p>
          </div>
        ) : (
          <div className="task-list-stack">
            {filtered.map((task) => (
              <TaskCard
                key={task.task_id}
                task={task}
                onPause={onPauseTask}
                onResume={onResumeTask}
                onCancel={onCancelTask}
                onDecideApproval={onDecideApproval}
                onRefreshAuthoritative={onRefresh}
                onOpenConversation={onOpenConversation}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
