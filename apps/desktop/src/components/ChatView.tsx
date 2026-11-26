import { useEffect, useMemo, useRef, useState } from 'react'
import type {
  ApprovalDecisionPayload,
  ChatMessage,
  ChatMode,
  ConversationDetail,
  TaskRecordView,
} from '../../shared/backend.ts'
import {
  CheckIcon,
  CopyIcon,
  DesktopIcon,
  RefreshIcon,
  RobbieEmblem,
} from './Icons.tsx'
import { MarkdownContent } from './MarkdownContent.tsx'
import { MessageComposer } from './MessageComposer.tsx'
import { TaskCard } from './TaskCard.tsx'

export interface ChatViewProps {
  conversation: ConversationDetail | null
  tasks: TaskRecordView[]
  draft: string
  mode: ChatMode
  sending: boolean
  backendReady: boolean
  emergencyStopped: boolean
  chatReady: boolean
  desktopReady: boolean
  sendError: string | null
  onDraftChange: (text: string) => void
  onModeChange: (mode: ChatMode) => void
  onSend: () => void
  onRetryMessage: (messageId: string) => Promise<void>
  onQuickPrompt: (prompt: string, mode: ChatMode) => void
  onPauseTask: (taskId: string) => Promise<void>
  onResumeTask: (taskId: string) => Promise<void>
  onCancelTask: (taskId: string) => Promise<void>
  onDecideApproval: (
    taskId: string,
    approvalId: string,
    decision: ApprovalDecisionPayload,
  ) => Promise<void>
  onRefreshAuthoritative: () => Promise<void>
  onOpenSettings: () => void
}

const STARTER_PROMPTS: Array<{
  title: string
  prompt: string
  mode: ChatMode
  tag: string
}> = [
  {
    title: 'Explain a technical topic',
    prompt: 'Summarize how Windows DPI awareness affects desktop automation coordinates.',
    mode: 'chat',
    tag: 'Chat',
  },
  {
    title: 'Draft a checklist or script',
    prompt: 'Write a concise PowerShell checklist for verifying local Python and Node environments.',
    mode: 'chat',
    tag: 'Chat',
  },
  {
    title: 'Launch a safe Windows app',
    prompt: 'Open Notepad on my desktop and wait for my next instruction.',
    mode: 'desktop',
    tag: 'Desktop',
  },
  {
    title: 'Guided desktop workflow',
    prompt: 'Open Calculator and check the current window layout.',
    mode: 'desktop',
    tag: 'Desktop',
  },
]

function formatTimestamp(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString([], {
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return ''
  }
}

function MessageRow({
  message,
  linkedTask,
  retrying,
  onRetry,
  onPauseTask,
  onResumeTask,
  onCancelTask,
  onDecideApproval,
  onRefreshAuthoritative,
}: {
  message: ChatMessage
  linkedTask: TaskRecordView | null
  retrying: boolean
  onRetry?: () => void
  onPauseTask: (taskId: string) => Promise<void>
  onResumeTask: (taskId: string) => Promise<void>
  onCancelTask: (taskId: string) => Promise<void>
  onDecideApproval: (
    taskId: string,
    approvalId: string,
    decision: ApprovalDecisionPayload,
  ) => Promise<void>
  onRefreshAuthoritative: () => Promise<void>
}) {
  const [copied, setCopied] = useState(false)
  const isUser = message.role === 'user'

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(message.content)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1600)
    } catch {
      setCopied(false)
    }
  }

  return (
    <div
      className={`message-row ${isUser ? 'message-row--user' : 'message-row--assistant'}`}
    >
      {isUser ? (
        <div className="user-bubble-wrap">
          <div className="user-bubble">
            <div className="user-bubble-text">{message.content}</div>
          </div>
          <div className="message-actions message-actions--user">
            <span className="message-time">
              {formatTimestamp(message.created_at)}
            </span>
            <button
              type="button"
              className="msg-action-btn"
              onClick={() => {
                void handleCopy()
              }}
              aria-label="Copy message"
              title="Copy message"
            >
              {copied ? <CheckIcon size={12} /> : <CopyIcon size={12} />}
              <span>{copied ? 'Copied' : 'Copy'}</span>
            </button>
            {onRetry ? (
              <button
                type="button"
                className="msg-action-btn"
                disabled={retrying}
                onClick={onRetry}
                aria-label="Retry message"
                title="Retry this prompt"
              >
                <RefreshIcon size={12} />
                <span>{retrying ? 'Retrying...' : 'Retry'}</span>
              </button>
            ) : null}
          </div>
        </div>
      ) : (
        <div className="assistant-message">
          <div className="assistant-avatar">
            <RobbieEmblem size={24} />
          </div>
          <div className="assistant-body">
            <div className="assistant-header">
              <span className="assistant-name">Robbie</span>
              {message.kind === 'task_started' ? (
                <span className="msg-kind-badge msg-kind-badge--task">
                  <DesktopIcon size={11} />
                  <span>Desktop Task</span>
                </span>
              ) : message.kind === 'task_summary' ? (
                <span className="msg-kind-badge msg-kind-badge--summary">
                  Task Outcome
                </span>
              ) : message.kind === 'error' ? (
                <span className="msg-kind-badge msg-kind-badge--error">
                  Notice
                </span>
              ) : null}
              <span className="message-time">
                {formatTimestamp(message.created_at)}
              </span>
            </div>

            <div
              className={`assistant-content ${message.kind === 'error' ? 'assistant-content--error' : ''}`}
            >
              <MarkdownContent content={message.content} />
            </div>

            {linkedTask ? (
              <div className="inline-task-slot">
                <TaskCard
                  task={linkedTask}
                  onPause={onPauseTask}
                  onResume={onResumeTask}
                  onCancel={onCancelTask}
                  onDecideApproval={onDecideApproval}
                  onRefreshAuthoritative={onRefreshAuthoritative}
                />
              </div>
            ) : null}

            <div className="message-actions">
              <button
                type="button"
                className="msg-action-btn"
                onClick={() => {
                  void handleCopy()
                }}
                aria-label="Copy response"
                title="Copy response"
              >
                {copied ? <CheckIcon size={12} /> : <CopyIcon size={12} />}
                <span>{copied ? 'Copied' : 'Copy'}</span>
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

export function ChatView({
  conversation,
  tasks,
  draft,
  mode,
  sending,
  backendReady,
  emergencyStopped,
  chatReady,
  desktopReady,
  sendError,
  onDraftChange,
  onModeChange,
  onSend,
  onRetryMessage,
  onQuickPrompt,
  onPauseTask,
  onResumeTask,
  onCancelTask,
  onDecideApproval,
  onRefreshAuthoritative,
  onOpenSettings,
}: ChatViewProps) {
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const [retryingId, setRetryingId] = useState<string | null>(null)

  const tasksById = useMemo(() => {
    const map = new Map<string, TaskRecordView>()
    for (const t of tasks) {
      map.set(t.task_id, t)
    }
    if (conversation) {
      for (const t of conversation.tasks) {
        if (!map.has(t.task_id)) {
          map.set(t.task_id, t)
        }
      }
    }
    return map
  }, [tasks, conversation])

  const messages = useMemo(
    () => conversation?.messages ?? [],
    [conversation],
  )

  // Track which tasks are already rendered inline under a `task_started` message
  const attachedTaskIds = useMemo(() => {
    const ids = new Set<string>()
    for (const msg of messages) {
      if (msg.kind === 'task_started' && msg.task_id) {
        ids.add(msg.task_id)
      }
    }
    return ids
  }, [messages])

  const unattachedConversationTasks = useMemo(() => {
    if (!conversation) return []
    const out: TaskRecordView[] = []
    for (const taskId of conversation.task_ids) {
      if (attachedTaskIds.has(taskId)) continue
      const found = tasksById.get(taskId)
      if (found) out.push(found)
    }
    return out
  }, [conversation, attachedTaskIds, tasksById])

  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
  }, [messages.length, sending, conversation?.conversation_id])

  const handleRetry = async (messageId: string) => {
    if (retryingId || sending) return
    setRetryingId(messageId)
    try {
      await onRetryMessage(messageId)
    } finally {
      setRetryingId(null)
    }
  }

  return (
    <div className="chat-layout">
      <div className="chat-scroll-region" ref={scrollRef}>
        <div className="chat-column">
          {!chatReady && messages.length === 0 ? (
            <div className="setup-callout">
              <div>
                <strong>Model configuration required for live responses.</strong>{' '}
                Configure your provider API keys in environment variables (or
                select a configured provider in Settings).
              </div>
              <button
                type="button"
                className="btn btn--secondary btn--sm"
                onClick={onOpenSettings}
              >
                Open Settings
              </button>
            </div>
          ) : null}

          {messages.length === 0 ? (
            <div className="chat-welcome">
              <div className="chat-welcome-icon">
                <RobbieEmblem size={42} />
              </div>
              <h2 className="chat-welcome-title">How can I help today?</h2>
              <p className="chat-welcome-sub">
                Chat naturally with Robbie or request safe, human-supervised
                Windows desktop actions powered by Agent-S.
              </p>

              <div className="starter-grid">
                {STARTER_PROMPTS.map((item) => (
                  <button
                    key={item.title}
                    type="button"
                    className="starter-card"
                    disabled={!backendReady || sending}
                    onClick={() => onQuickPrompt(item.prompt, item.mode)}
                  >
                    <div className="starter-card-top">
                      <span className="starter-card-title">{item.title}</span>
                      <span
                        className={`starter-tag ${item.mode === 'desktop' ? 'starter-tag--desktop' : ''}`}
                      >
                        {item.tag}
                      </span>
                    </div>
                    <div className="starter-card-prompt">{item.prompt}</div>
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className="message-list">
              {messages.map((msg) => {
                const linkedTask =
                  msg.kind === 'task_started' && msg.task_id
                    ? (tasksById.get(msg.task_id) ?? null)
                    : null
                return (
                  <MessageRow
                    key={msg.message_id}
                    message={msg}
                    linkedTask={linkedTask}
                    retrying={retryingId === msg.message_id}
                    onRetry={
                      msg.role === 'user'
                        ? () => {
                            void handleRetry(msg.message_id)
                          }
                        : undefined
                    }
                    onPauseTask={onPauseTask}
                    onResumeTask={onResumeTask}
                    onCancelTask={onCancelTask}
                    onDecideApproval={onDecideApproval}
                    onRefreshAuthoritative={onRefreshAuthoritative}
                  />
                )
              })}

              {unattachedConversationTasks.map((task) => (
                <div key={task.task_id} className="inline-task-slot">
                  <TaskCard
                    task={task}
                    onPause={onPauseTask}
                    onResume={onResumeTask}
                    onCancel={onCancelTask}
                    onDecideApproval={onDecideApproval}
                    onRefreshAuthoritative={onRefreshAuthoritative}
                  />
                </div>
              ))}

              {sending ? (
                <div className="message-row message-row--assistant">
                  <div className="assistant-message">
                    <div className="assistant-avatar">
                      <RobbieEmblem size={24} />
                    </div>
                    <div className="assistant-body">
                      <div className="processing-indicator" role="status">
                        <span className="spinner-dots" aria-hidden="true">
                          <span />
                          <span />
                          <span />
                        </span>
                        <span>
                          {mode === 'desktop'
                            ? 'Submitting desktop task to backend...'
                            : 'Robbie is processing your request...'}
                        </span>
                      </div>
                    </div>
                  </div>
                </div>
              ) : null}
            </div>
          )}
        </div>
      </div>

      <MessageComposer
        value={draft}
        mode={mode}
        disabled={!backendReady}
        sending={sending}
        emergencyStopped={emergencyStopped}
        desktopReady={desktopReady}
        error={sendError}
        onChange={onDraftChange}
        onModeChange={onModeChange}
        onSubmit={onSend}
      />
    </div>
  )
}
