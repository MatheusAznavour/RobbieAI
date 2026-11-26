import type {
  ChatMessage,
  ConversationSummary,
  PendingApproval,
  TaskEventItem,
  TaskLifecycleState,
  TaskRecordView,
} from '../../shared/backend.ts'

const MAX_EVENTS_PER_TASK = 50

const TERMINAL_TASK_STATES: ReadonlySet<TaskLifecycleState> = new Set([
  'completed',
  'failed',
  'cancelled',
  'emergency_stopped',
])

export function isTerminalTaskState(state: TaskLifecycleState): boolean {
  return TERMINAL_TASK_STATES.has(state)
}

export function isActiveTaskState(state: TaskLifecycleState): boolean {
  return !TERMINAL_TASK_STATES.has(state)
}

/**
 * Produces a stable deduplication key for a task event so duplicate WebSocket
 * broadcasts or overlapping HTTP + WS updates never duplicate timeline items.
 */
export function taskEventKey(event: TaskEventItem): string {
  return [
    event.type,
    event.timestamp ?? '',
    event.task_id ?? '',
    event.step_id ?? '',
    event.approval_id ?? event.approval?.approval_id ?? '',
    event.result_status ?? '',
    event.detail ?? event.error ?? event.reason ?? '',
  ].join('|')
}

export function mergeTaskEvents(
  existing: TaskEventItem[],
  incoming: TaskEventItem[],
): TaskEventItem[] {
  const seen = new Set<string>()
  const merged: TaskEventItem[] = []
  for (const item of [...existing, ...incoming]) {
    const key = taskEventKey(item)
    if (!seen.has(key)) {
      seen.add(key)
      merged.push(item)
    }
  }
  merged.sort((a, b) => (a.timestamp || '').localeCompare(b.timestamp || ''))
  if (merged.length > MAX_EVENTS_PER_TASK) {
    return merged.slice(merged.length - MAX_EVENTS_PER_TASK)
  }
  return merged
}

export function upsertConversationSummary(
  list: ConversationSummary[],
  incoming: ConversationSummary,
): ConversationSummary[] {
  const filtered = list.filter(
    (item) => item.conversation_id !== incoming.conversation_id,
  )
  return [incoming, ...filtered].sort((a, b) =>
    b.updated_at.localeCompare(a.updated_at),
  )
}

export function appendMessageDeduplicated(
  messages: ChatMessage[],
  incoming: ChatMessage,
): ChatMessage[] {
  const existingIdx = messages.findIndex(
    (m) => m.message_id === incoming.message_id,
  )
  if (existingIdx !== -1) {
    const next = [...messages]
    next[existingIdx] = incoming
    return next
  }
  return [...messages, incoming]
}

export function upsertTaskRecord(
  tasks: TaskRecordView[],
  incoming: TaskRecordView,
): TaskRecordView[] {
  const existing = tasks.find((t) => t.task_id === incoming.task_id)
  const mergedEvents = existing
    ? mergeTaskEvents(existing.events, incoming.events ?? [])
    : mergeTaskEvents([], incoming.events ?? [])

  const merged: TaskRecordView = {
    ...incoming,
    events: mergedEvents,
  }

  const others = tasks.filter((t) => t.task_id !== incoming.task_id)
  return [merged, ...others].sort((a, b) =>
    b.updated_at.localeCompare(a.updated_at),
  )
}

function eventToTaskState(event: TaskEventItem): TaskLifecycleState | null {
  switch (event.type) {
    case 'task_started':
    case 'step_started':
    case 'task_resumed':
      return 'running'
    case 'task_paused':
      return 'paused'
    case 'approval_required':
      return 'awaiting_approval'
    case 'approval_resolved':
      return event.approved ? 'running' : 'cancelled'
    case 'task_completed':
      return 'completed'
    case 'task_failed':
      return 'failed'
    case 'task_cancelled':
      return 'cancelled'
    case 'task_emergency_stopped':
      return 'emergency_stopped'
    default:
      return null
  }
}

/**
 * Reconciles a real-time WebSocket task event into the local task list safely.
 * Never lets stale non-terminal events resurrect an already-terminal task.
 */
export function applyTaskEvent(
  tasks: TaskRecordView[],
  event: TaskEventItem,
): TaskRecordView[] {
  const taskId = event.task_id
  if (!taskId) {
    return tasks
  }

  const existing = tasks.find((t) => t.task_id === taskId)
  const nextState = eventToTaskState(event)
  const ts = event.timestamp || new Date().toISOString()

  if (!existing) {
    const created: TaskRecordView = {
      task_id: taskId,
      conversation_id: event.conversation_id ?? null,
      instruction: '',
      state: nextState ?? 'running',
      current_step: typeof event.step_index === 'number' ? event.step_index : 0,
      max_steps: 0,
      last_step_id: event.step_id ?? null,
      last_error: event.error ?? null,
      created_at: ts,
      updated_at: ts,
      pending_approval:
        event.type === 'approval_required' ? (event.approval ?? null) : null,
      events: [event],
    }
    return upsertTaskRecord(tasks, created)
  }

  // Prevent out-of-order non-terminal events from overriding a terminal state
  const resolvedState =
    isTerminalTaskState(existing.state) &&
    nextState !== null &&
    !isTerminalTaskState(nextState)
      ? existing.state
      : (nextState ?? existing.state)

  let pendingApproval: PendingApproval | null = existing.pending_approval
  if (event.type === 'approval_required' && event.approval) {
    if (!isTerminalTaskState(resolvedState)) {
      pendingApproval = event.approval
    }
  } else if (
    event.type === 'approval_resolved' ||
    isTerminalTaskState(resolvedState)
  ) {
    pendingApproval = null
  }

  const updated: TaskRecordView = {
    ...existing,
    state: resolvedState,
    current_step:
      typeof event.step_index === 'number'
        ? Math.max(existing.current_step, event.step_index)
        : existing.current_step,
    last_step_id: event.step_id ?? existing.last_step_id,
    last_error: event.error ?? existing.last_error,
    updated_at: ts >= existing.updated_at ? ts : existing.updated_at,
    pending_approval: pendingApproval,
    events: mergeTaskEvents(existing.events, [event]),
  }

  return upsertTaskRecord(tasks, updated)
}

export interface ApprovalWithTaskContext {
  approval: PendingApproval
  task: TaskRecordView
}

export function collectPendingApprovals(
  tasks: TaskRecordView[],
): ApprovalWithTaskContext[] {
  const result: ApprovalWithTaskContext[] = []
  for (const task of tasks) {
    if (task.state === 'awaiting_approval' && task.pending_approval) {
      result.push({ approval: task.pending_approval, task })
    }
  }
  result.sort((a, b) =>
    b.approval.created_at.localeCompare(a.approval.created_at),
  )
  return result
}

export function isConflictError(err: unknown): boolean {
  const message = err instanceof Error ? err.message : String(err ?? '')
  return /HTTP\s+409\b/i.test(message) || /\b409\b/.test(message)
}

export function updateDraftMap(
  drafts: Record<string, string>,
  conversationId: string,
  text: string,
): Record<string, string> {
  if (drafts[conversationId] === text) {
    return drafts
  }
  if (!text) {
    const next = { ...drafts }
    delete next[conversationId]
    return next
  }
  return {
    ...drafts,
    [conversationId]: text,
  }
}
