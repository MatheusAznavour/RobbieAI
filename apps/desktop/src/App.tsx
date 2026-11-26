import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type {
  ApprovalDecisionPayload,
  BackendEvent,
  BackendStatusSnapshot,
  ChatMode,
  ConversationDetail,
  ConversationSummary,
  ModelSelectionState,
  SettingsSnapshot,
  TaskEventItem,
  TaskRecordView,
} from '../shared/backend.ts'
import { ApprovalsView } from './components/ApprovalsView.tsx'
import { ChatView } from './components/ChatView.tsx'
import { EmergencyStopBar } from './components/EmergencyStopBar.tsx'
import { SettingsView } from './components/SettingsView.tsx'
import { Sidebar, type ActiveScreen } from './components/Sidebar.tsx'
import { TasksView } from './components/TasksView.tsx'
import { robbieClient } from './lib/robbieClient.ts'
import {
  appendMessageDeduplicated,
  applyTaskEvent,
  collectPendingApprovals,
  isActiveTaskState,
  updateDraftMap,
  upsertConversationSummary,
  upsertTaskRecord,
} from './lib/stateReconciler.ts'
import './App.css'

const NEW_DRAFT_KEY = '__new__'

const INITIAL_STATUS: BackendStatusSnapshot = {
  state: 'starting',
  host: null,
  port: null,
  pid: null,
  version: null,
  wsConnected: false,
  emergencyStopped: false,
  lastError: null,
  updatedAt: new Date().toISOString(),
}

const TASK_EVENT_TYPES = new Set([
  'task_started',
  'action_planned',
  'approval_required',
  'approval_resolved',
  'action_executed',
  'task_paused',
  'task_resumed',
  'task_completed',
  'task_failed',
  'task_cancelled',
  'task_emergency_stopped',
])

function isTaskEvent(event: BackendEvent): event is TaskEventItem {
  return TASK_EVENT_TYPES.has(event.type)
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

function App() {
  const [backendStatus, setBackendStatus] =
    useState<BackendStatusSnapshot>(INITIAL_STATUS)
  const [activeScreen, setActiveScreen] = useState<ActiveScreen>('chat')

  const [conversations, setConversations] = useState<ConversationSummary[]>([])
  const [searchQuery, setSearchQuery] = useState('')
  const [activeConversation, setActiveConversation] =
    useState<ConversationDetail | null>(null)
  const [tasks, setTasks] = useState<TaskRecordView[]>([])
  const [settings, setSettings] = useState<SettingsSnapshot | null>(null)

  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [chatMode, setChatMode] = useState<ChatMode>('auto')
  const [sending, setSending] = useState(false)
  const [sendError, setSendError] = useState<string | null>(null)

  const [loadingTasks, setLoadingTasks] = useState(false)
  const [loadingSettings, setLoadingSettings] = useState(false)
  const [restartingBackend, setRestartingBackend] = useState(false)

  const [emergencyPending, setEmergencyPending] = useState(false)
  const [resetPending, setResetPending] = useState(false)
  const [emergencyFeedback, setEmergencyFeedback] = useState<string | null>(null)

  const activeConversationIdRef = useRef<string | null>(null)
  const searchQueryRef = useRef<string>('')
  const prevWsConnectedRef = useRef<boolean>(false)
  const initializedRef = useRef<boolean>(false)

  activeConversationIdRef.current = activeConversation?.conversation_id ?? null
  searchQueryRef.current = searchQuery

  const refreshConversations = useCallback(async (query?: string) => {
    const q = query !== undefined ? query : searchQueryRef.current
    const list = await robbieClient.listConversations(q.trim() || undefined)
    setConversations(list)
    return list
  }, [])

  const refreshTasks = useCallback(async () => {
    setLoadingTasks(true)
    try {
      const list = await robbieClient.listTasks()
      setTasks(list)
      return list
    } finally {
      setLoadingTasks(false)
    }
  }, [])

  const refreshSettings = useCallback(async () => {
    setLoadingSettings(true)
    try {
      const snap = await robbieClient.getSettings()
      setSettings(snap)
      return snap
    } finally {
      setLoadingSettings(false)
    }
  }, [])

  const loadConversation = useCallback(async (conversationId: string) => {
    const detail = await robbieClient.getConversation(conversationId)
    setActiveConversation(detail)
    if (detail.tasks.length > 0) {
      setTasks((prev) => {
        let next = prev
        for (const t of detail.tasks) {
          next = upsertTaskRecord(next, t)
        }
        return next
      })
    }
    return detail
  }, [])

  const reconcileAll = useCallback(async () => {
    try {
      const [list] = await Promise.all([
        refreshConversations(),
        refreshTasks(),
        refreshSettings(),
      ])

      const currentId = activeConversationIdRef.current
      if (currentId) {
        if (list.some((c) => c.conversation_id === currentId)) {
          await loadConversation(currentId)
        } else {
          setActiveConversation(null)
        }
      } else if (!initializedRef.current && list.length > 0) {
        initializedRef.current = true
        await loadConversation(list[0].conversation_id)
      }
      initializedRef.current = true
    } catch (err) {
      setSendError(errorMessage(err))
    }
  }, [loadConversation, refreshConversations, refreshSettings, refreshTasks])

  // Subscribe to Electron Main bridge status & WebSocket events
  useEffect(() => {
    let mounted = true

    robbieClient
      .getBackendStatus()
      .then((snapshot) => {
        if (!mounted) return
        setBackendStatus(snapshot)
        prevWsConnectedRef.current = snapshot.wsConnected
        if (snapshot.state === 'ready') {
          void reconcileAll()
        }
      })
      .catch((err: unknown) => {
        if (!mounted) return
        setSendError(errorMessage(err))
      })

    const unsubStatus = robbieClient.onBackendStatusChange((snapshot) => {
      if (!mounted) return
      const wasConnected = prevWsConnectedRef.current
      prevWsConnectedRef.current = snapshot.wsConnected
      setBackendStatus(snapshot)

      if (snapshot.state === 'ready' && (!wasConnected || !initializedRef.current)) {
        void reconcileAll()
      }
    })

    const unsubEvent = robbieClient.onBackendEvent((event: BackendEvent) => {
      if (!mounted) return

      if (event.type === 'conversation_message') {
        const msg = event.message
        if (activeConversationIdRef.current === event.conversation_id) {
          setActiveConversation((prev) => {
            if (!prev || prev.conversation_id !== event.conversation_id) {
              return prev
            }
            return {
              ...prev,
              updated_at: msg.created_at,
              messages: appendMessageDeduplicated(prev.messages, msg),
            }
          })
        }
        void refreshConversations()
        return
      }

      if (event.type === 'emergency_stop') {
        const count = event.affected_task_ids.length
        setEmergencyFeedback(
          count > 0
            ? `${count} running task${count === 1 ? '' : 's'} halted immediately`
            : 'Latch engaged',
        )
        void refreshTasks()
        if (activeConversationIdRef.current) {
          void loadConversation(activeConversationIdRef.current)
        }
        return
      }

      if (event.type === 'emergency_stop_reset') {
        setEmergencyFeedback('Emergency stop latch reset. Ready for new desktop tasks.')
        window.setTimeout(() => {
          setEmergencyFeedback((prev) =>
            prev === 'Emergency stop latch reset. Ready for new desktop tasks.'
              ? null
              : prev,
          )
        }, 4500)
        return
      }

      if (isTaskEvent(event)) {
        setTasks((prev) => applyTaskEvent(prev, event))
        if (
          event.task_id &&
          (event.type === 'approval_required' ||
            event.type === 'task_completed' ||
            event.type === 'task_failed' ||
            event.type === 'task_cancelled' ||
            event.type === 'task_emergency_stopped')
        ) {
          robbieClient
            .getTask(event.task_id)
            .then((authoritative) => {
              if (!mounted) return
              setTasks((prev) => upsertTaskRecord(prev, authoritative))
            })
            .catch(() => {
              // Event already reconciled locally
            })
          if (
            activeConversationIdRef.current &&
            event.conversation_id === activeConversationIdRef.current
          ) {
            void loadConversation(activeConversationIdRef.current)
          }
        }
      }
    })

    return () => {
      mounted = false
      unsubStatus()
      unsubEvent()
    }
  }, [loadConversation, reconcileAll, refreshConversations, refreshTasks])

  // Search conversations reactively
  useEffect(() => {
    if (backendStatus.state !== 'ready') return
    const timer = window.setTimeout(() => {
      void refreshConversations(searchQuery).catch(() => undefined)
    }, 150)
    return () => window.clearTimeout(timer)
  }, [searchQuery, backendStatus.state, refreshConversations])

  const handleEmergencyStop = useCallback(async () => {
    if (emergencyPending) return
    setEmergencyPending(true)
    setEmergencyFeedback(null)
    try {
      const resp = await robbieClient.emergencyStop()
      setEmergencyFeedback(
        resp.affected_task_ids.length > 0
          ? `Halted ${resp.affected_task_ids.length} task(s)`
          : 'Latch engaged',
      )
      await refreshTasks()
      if (activeConversationIdRef.current) {
        await loadConversation(activeConversationIdRef.current)
      }
    } catch (err) {
      setEmergencyFeedback(`Emergency stop error: ${errorMessage(err)}`)
    } finally {
      setEmergencyPending(false)
    }
  }, [emergencyPending, loadConversation, refreshTasks])

  const handleResetEmergencyStop = useCallback(async () => {
    if (resetPending) return
    setResetPending(true)
    try {
      await robbieClient.resetEmergencyStop()
      setEmergencyFeedback('Emergency stop latch reset. Stopped tasks remain stopped.')
    } catch (err) {
      setEmergencyFeedback(`Reset failed: ${errorMessage(err)}`)
    } finally {
      setResetPending(false)
    }
  }, [resetPending])

  // Keyboard shortcuts: Ctrl+N new chat, Ctrl+Alt+Esc emergency stop fallback
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.altKey && e.key === 'Escape') {
        e.preventDefault()
        void handleEmergencyStop()
        return
      }
      if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'n') {
        e.preventDefault()
        setActiveConversation(null)
        setActiveScreen('chat')
        setSendError(null)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [handleEmergencyStop])

  const currentDraftKey = activeConversation?.conversation_id ?? NEW_DRAFT_KEY
  const currentDraft = drafts[currentDraftKey] ?? ''

  const handleDraftChange = useCallback(
    (text: string) => {
      setDrafts((prev) => updateDraftMap(prev, currentDraftKey, text))
      if (sendError) setSendError(null)
    },
    [currentDraftKey, sendError],
  )

  const handleNewChat = useCallback(() => {
    setActiveConversation(null)
    setActiveScreen('chat')
    setSendError(null)
  }, [])

  const handleSelectConversation = useCallback(
    async (conversationId: string) => {
      setActiveScreen('chat')
      setSendError(null)
      try {
        await loadConversation(conversationId)
      } catch (err) {
        setSendError(errorMessage(err))
      }
    },
    [loadConversation],
  )

  const handleRenameConversation = useCallback(
    async (conversationId: string, title: string) => {
      const updated = await robbieClient.renameConversation(conversationId, title)
      setConversations((prev) => upsertConversationSummary(prev, updated))
      setActiveConversation((prev) =>
        prev && prev.conversation_id === conversationId
          ? { ...prev, title: updated.title, updated_at: updated.updated_at }
          : prev,
      )
    },
    [],
  )

  const handleDeleteConversation = useCallback(
    async (conversationId: string) => {
      await robbieClient.deleteConversation(conversationId)
      setConversations((prev) =>
        prev.filter((c) => c.conversation_id !== conversationId),
      )
      setDrafts((prev) => updateDraftMap(prev, conversationId, ''))
      if (activeConversationIdRef.current === conversationId) {
        setActiveConversation(null)
      }
    },
    [],
  )

  const sendText = useCallback(
    async (rawText: string, modeToUse: ChatMode) => {
      const content = rawText.trim()
      if (!content || sending || backendStatus.state !== 'ready') return

      setSending(true)
      setSendError(null)

      try {
        let targetConvId = activeConversationIdRef.current
        if (!targetConvId) {
          const created = await robbieClient.createConversation()
          targetConvId = created.conversation_id
          setActiveConversation(created)
        }

        const resp = await robbieClient.sendMessage(targetConvId, content, modeToUse)

        setConversations((prev) =>
          upsertConversationSummary(prev, resp.conversation),
        )

        if (resp.task) {
          setTasks((prev) => upsertTaskRecord(prev, resp.task!))
        }

        setActiveConversation((prev) => {
          const base: ConversationDetail =
            prev && prev.conversation_id === targetConvId
              ? prev
              : {
                  conversation_id: targetConvId,
                  title: resp.conversation.title,
                  created_at: resp.conversation.created_at,
                  updated_at: resp.conversation.updated_at,
                  messages: [],
                  task_ids: [],
                  tasks: [],
                }

          let msgs = appendMessageDeduplicated(base.messages, resp.user_message)
          msgs = appendMessageDeduplicated(msgs, resp.assistant_message)
          const taskIds =
            resp.task && !base.task_ids.includes(resp.task.task_id)
              ? [...base.task_ids, resp.task.task_id]
              : base.task_ids
          const convTasks = resp.task
            ? upsertTaskRecord(base.tasks, resp.task)
            : base.tasks

          return {
            ...base,
            title: resp.conversation.title,
            updated_at: resp.conversation.updated_at,
            messages: msgs,
            task_ids: taskIds,
            tasks: convTasks,
          }
        })

        // Clear draft ONLY after confirmed delivery
        setDrafts((prev) => {
          let next = updateDraftMap(prev, targetConvId, '')
          next = updateDraftMap(next, NEW_DRAFT_KEY, '')
          return next
        })
      } catch (err) {
        setSendError(errorMessage(err))
      } finally {
        setSending(false)
      }
    },
    [backendStatus.state, sending],
  )

  const handleSend = useCallback(() => {
    void sendText(currentDraft, chatMode)
  }, [chatMode, currentDraft, sendText])

  const handleQuickPrompt = useCallback(
    (prompt: string, modeForPrompt: ChatMode) => {
      setChatMode(modeForPrompt)
      void sendText(prompt, modeForPrompt)
    },
    [sendText],
  )

  const handleRetryMessage = useCallback(async (messageId: string) => {
    const convId = activeConversationIdRef.current
    if (!convId) return
    setSending(true)
    setSendError(null)
    try {
      const resp = await robbieClient.retryMessage(convId, messageId)
      setConversations((prev) =>
        upsertConversationSummary(prev, resp.conversation),
      )
      if (resp.task) {
        setTasks((prev) => upsertTaskRecord(prev, resp.task!))
      }
      await loadConversation(convId)
    } catch (err) {
      setSendError(errorMessage(err))
    } finally {
      setSending(false)
    }
  }, [loadConversation])

  const handlePauseTask = useCallback(async (taskId: string) => {
    const updated = await robbieClient.pauseTask(taskId)
    setTasks((prev) => upsertTaskRecord(prev, updated))
  }, [])

  const handleResumeTask = useCallback(async (taskId: string) => {
    const updated = await robbieClient.resumeTask(taskId)
    setTasks((prev) => upsertTaskRecord(prev, updated))
  }, [])

  const handleCancelTask = useCallback(async (taskId: string) => {
    const updated = await robbieClient.cancelTask(taskId)
    setTasks((prev) => upsertTaskRecord(prev, updated))
  }, [])

  const handleDecideApproval = useCallback(
    async (
      taskId: string,
      approvalId: string,
      decision: ApprovalDecisionPayload,
    ) => {
      const updated = await robbieClient.resolveApproval(
        taskId,
        approvalId,
        decision,
      )
      setTasks((prev) => upsertTaskRecord(prev, updated))
      if (activeConversationIdRef.current) {
        await loadConversation(activeConversationIdRef.current)
      }
    },
    [loadConversation],
  )

  const handleSaveSettings = useCallback(
    async (selection: ModelSelectionState) => {
      const updated = await robbieClient.updateSettings(selection)
      setSettings(updated)
    },
    [],
  )

  const handleRestartBackend = useCallback(async () => {
    if (restartingBackend) return
    setRestartingBackend(true)
    try {
      const snap = await robbieClient.restartBackend()
      setBackendStatus(snap)
      await reconcileAll()
    } catch (err) {
      setSendError(errorMessage(err))
    } finally {
      setRestartingBackend(false)
    }
  }, [reconcileAll, restartingBackend])

  const pendingApprovals = useMemo(
    () => collectPendingApprovals(tasks),
    [tasks],
  )

  const activeTaskCount = useMemo(
    () => tasks.filter((t) => isActiveTaskState(t.state)).length,
    [tasks],
  )

  const topbarTitle = useMemo(() => {
    switch (activeScreen) {
      case 'chat':
        return activeConversation?.title ?? 'New Conversation'
      case 'approvals':
        return 'Human Approval Center'
      case 'tasks':
        return 'Desktop Tasks'
      case 'settings':
        return 'Settings'
    }
  }, [activeScreen, activeConversation?.title])

  const topbarSubtitle = useMemo(() => {
    if (activeScreen === 'chat' && settings) {
      return `${settings.selection.chat_provider} / ${settings.selection.chat_model}`
    }
    return undefined
  }, [activeScreen, settings])

  return (
    <div className="robbie-app">
      <Sidebar
        activeScreen={activeScreen}
        conversations={conversations}
        activeConversationId={activeConversation?.conversation_id ?? null}
        searchQuery={searchQuery}
        pendingApprovalCount={pendingApprovals.length}
        activeTaskCount={activeTaskCount}
        backendStatus={backendStatus}
        restartingBackend={restartingBackend}
        onSelectScreen={setActiveScreen}
        onNewChat={handleNewChat}
        onSelectConversation={(id) => {
          void handleSelectConversation(id)
        }}
        onSearchChange={setSearchQuery}
        onRenameConversation={handleRenameConversation}
        onDeleteConversation={handleDeleteConversation}
        onRestartBackend={() => {
          void handleRestartBackend()
        }}
      />

      <div className="main-stage">
        <EmergencyStopBar
          title={topbarTitle}
          subtitle={topbarSubtitle}
          backendStatus={backendStatus}
          pendingApprovalCount={pendingApprovals.length}
          activeTaskCount={activeTaskCount}
          emergencyPending={emergencyPending}
          resetPending={resetPending}
          emergencyFeedback={emergencyFeedback}
          onEmergencyStop={() => {
            void handleEmergencyStop()
          }}
          onResetEmergencyStop={() => {
            void handleResetEmergencyStop()
          }}
          onOpenApprovals={() => setActiveScreen('approvals')}
          onOpenTasks={() => setActiveScreen('tasks')}
        />

        <main className="stage-body">
          {activeScreen === 'chat' ? (
            <ChatView
              conversation={activeConversation}
              tasks={tasks}
              draft={currentDraft}
              mode={chatMode}
              sending={sending}
              backendReady={backendStatus.state === 'ready'}
              emergencyStopped={backendStatus.emergencyStopped}
              chatReady={settings?.chat_ready ?? true}
              desktopReady={settings?.desktop_ready ?? false}
              sendError={sendError}
              onDraftChange={handleDraftChange}
              onModeChange={setChatMode}
              onSend={handleSend}
              onRetryMessage={handleRetryMessage}
              onQuickPrompt={handleQuickPrompt}
              onPauseTask={handlePauseTask}
              onResumeTask={handleResumeTask}
              onCancelTask={handleCancelTask}
              onDecideApproval={handleDecideApproval}
              onRefreshAuthoritative={reconcileAll}
              onOpenSettings={() => setActiveScreen('settings')}
            />
          ) : null}

          {activeScreen === 'approvals' ? (
            <ApprovalsView
              approvals={pendingApprovals}
              loading={loadingTasks}
              onDecideApproval={handleDecideApproval}
              onRefresh={async () => {
                await refreshTasks()
              }}
              onOpenConversation={(id) => {
                void handleSelectConversation(id)
              }}
            />
          ) : null}

          {activeScreen === 'tasks' ? (
            <TasksView
              tasks={tasks}
              loading={loadingTasks}
              emergencyStopped={backendStatus.emergencyStopped}
              onRefresh={async () => {
                await refreshTasks()
              }}
              onPauseTask={handlePauseTask}
              onResumeTask={handleResumeTask}
              onCancelTask={handleCancelTask}
              onDecideApproval={handleDecideApproval}
              onOpenConversation={(id) => {
                void handleSelectConversation(id)
              }}
            />
          ) : null}

          {activeScreen === 'settings' ? (
            <SettingsView
              settings={settings}
              loading={loadingSettings}
              restartingBackend={restartingBackend}
              onSave={handleSaveSettings}
              onRefresh={async () => {
                await refreshSettings()
              }}
              onRestartBackend={() => {
                void handleRestartBackend()
              }}
            />
          ) : null}
        </main>
      </div>
    </div>
  )
}

export default App
