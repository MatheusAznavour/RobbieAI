import { useState } from 'react'
import type {
  BackendStatusSnapshot,
  ConversationSummary,
} from '../../shared/backend.ts'
import {
  ChatIcon,
  CheckIcon,
  EditIcon,
  PlusIcon,
  RefreshIcon,
  RobbieEmblem,
  SearchIcon,
  SettingsIcon,
  ShieldIcon,
  TasksIcon,
  TrashIcon,
  XIcon,
} from './Icons.tsx'

export type ActiveScreen = 'chat' | 'approvals' | 'tasks' | 'settings'

export interface SidebarProps {
  activeScreen: ActiveScreen
  conversations: ConversationSummary[]
  activeConversationId: string | null
  searchQuery: string
  pendingApprovalCount: number
  activeTaskCount: number
  backendStatus: BackendStatusSnapshot
  restartingBackend: boolean
  onSelectScreen: (screen: ActiveScreen) => void
  onNewChat: () => void
  onSelectConversation: (conversationId: string) => void
  onSearchChange: (query: string) => void
  onRenameConversation: (conversationId: string, title: string) => Promise<void>
  onDeleteConversation: (conversationId: string) => Promise<void>
  onRestartBackend: () => void
}

function backendConnectionLabel(status: BackendStatusSnapshot): string {
  switch (status.state) {
    case 'ready':
      return status.wsConnected ? 'Backend Connected' : 'HTTP Only (WS Reconnecting)'
    case 'starting':
      return 'Starting Backend...'
    case 'restarting':
      return 'Restarting Backend...'
    case 'stopping':
      return 'Stopping Backend...'
    case 'error':
      return 'Backend Error'
    default:
      return 'Backend Offline'
  }
}

export function Sidebar({
  activeScreen,
  conversations,
  activeConversationId,
  searchQuery,
  pendingApprovalCount,
  activeTaskCount,
  backendStatus,
  restartingBackend,
  onSelectScreen,
  onNewChat,
  onSelectConversation,
  onSearchChange,
  onRenameConversation,
  onDeleteConversation,
  onRestartBackend,
}: SidebarProps) {
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editingTitle, setEditingTitle] = useState('')
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)

  const startEditing = (conv: ConversationSummary) => {
    setConfirmDeleteId(null)
    setEditingId(conv.conversation_id)
    setEditingTitle(conv.title)
  }

  const commitRename = async (conversationId: string) => {
    const trimmed = editingTitle.trim()
    if (!trimmed) {
      setEditingId(null)
      return
    }
    setBusyId(conversationId)
    try {
      await onRenameConversation(conversationId, trimmed)
    } finally {
      setBusyId(null)
      setEditingId(null)
    }
  }

  const commitDelete = async (conversationId: string) => {
    setBusyId(conversationId)
    try {
      await onDeleteConversation(conversationId)
    } finally {
      setBusyId(null)
      setConfirmDeleteId(null)
    }
  }

  const isHealthy = backendStatus.state === 'ready' && backendStatus.wsConnected

  return (
    <aside className="sidebar" aria-label="Robbie navigation and conversations">
      <div className="sidebar-brand">
        <div className="sidebar-brand-identity">
          <RobbieEmblem size={26} />
          <div>
            <div className="sidebar-brand-name">Robbie</div>
            <div className="sidebar-brand-sub">Desktop AI Assistant</div>
          </div>
        </div>
        <button
          type="button"
          className="btn btn--primary btn--sm new-chat-btn"
          onClick={onNewChat}
          title="Start a new conversation (Ctrl+N)"
        >
          <PlusIcon size={14} />
          <span>New Chat</span>
        </button>
      </div>

      <nav className="sidebar-nav" aria-label="Primary views">
        <button
          type="button"
          className={`nav-item ${activeScreen === 'chat' ? 'nav-item--active' : ''}`}
          onClick={() => onSelectScreen('chat')}
        >
          <ChatIcon size={15} />
          <span className="nav-item-label">Chat</span>
        </button>

        <button
          type="button"
          className={`nav-item ${activeScreen === 'approvals' ? 'nav-item--active' : ''}`}
          onClick={() => onSelectScreen('approvals')}
        >
          <ShieldIcon size={15} />
          <span className="nav-item-label">Approvals</span>
          {pendingApprovalCount > 0 ? (
            <span className="nav-badge nav-badge--warning">
              {pendingApprovalCount}
            </span>
          ) : null}
        </button>

        <button
          type="button"
          className={`nav-item ${activeScreen === 'tasks' ? 'nav-item--active' : ''}`}
          onClick={() => onSelectScreen('tasks')}
        >
          <TasksIcon size={15} />
          <span className="nav-item-label">Tasks</span>
          {activeTaskCount > 0 ? (
            <span className="nav-badge nav-badge--info">{activeTaskCount}</span>
          ) : null}
        </button>

        <button
          type="button"
          className={`nav-item ${activeScreen === 'settings' ? 'nav-item--active' : ''}`}
          onClick={() => onSelectScreen('settings')}
        >
          <SettingsIcon size={15} />
          <span className="nav-item-label">Settings</span>
        </button>
      </nav>

      <div className="sidebar-section-header">
        <span>Conversations</span>
        <span className="sidebar-count">{conversations.length}</span>
      </div>

      <div className="sidebar-search">
        <SearchIcon size={14} className="sidebar-search-icon" />
        <input
          type="search"
          value={searchQuery}
          onChange={(e) => onSearchChange(e.target.value)}
          placeholder="Search history..."
          aria-label="Search conversations"
          className="sidebar-search-input"
        />
      </div>

      <div className="conversation-list" role="list">
        {conversations.length === 0 ? (
          <div className="conversation-empty">
            {searchQuery.trim()
              ? 'No conversations match your search.'
              : 'No conversations yet. Start a new chat above.'}
          </div>
        ) : (
          conversations.map((conv) => {
            const isActive =
              activeScreen === 'chat' &&
              conv.conversation_id === activeConversationId
            const isEditing = editingId === conv.conversation_id
            const isConfirmingDelete = confirmDeleteId === conv.conversation_id
            const isBusy = busyId === conv.conversation_id

            return (
              <div
                key={conv.conversation_id}
                role="listitem"
                className={`conv-item ${isActive ? 'conv-item--active' : ''}`}
              >
                {isEditing ? (
                  <div className="conv-edit-row">
                    <input
                      type="text"
                      className="conv-edit-input"
                      value={editingTitle}
                      onChange={(e) => setEditingTitle(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') {
                          void commitRename(conv.conversation_id)
                        } else if (e.key === 'Escape') {
                          setEditingId(null)
                        }
                      }}
                      autoFocus
                      maxLength={120}
                      aria-label="Rename conversation"
                    />
                    <button
                      type="button"
                      className="icon-btn"
                      disabled={isBusy}
                      onClick={() => {
                        void commitRename(conv.conversation_id)
                      }}
                      aria-label="Save title"
                    >
                      <CheckIcon size={13} />
                    </button>
                    <button
                      type="button"
                      className="icon-btn"
                      onClick={() => setEditingId(null)}
                      aria-label="Cancel rename"
                    >
                      <XIcon size={13} />
                    </button>
                  </div>
                ) : isConfirmingDelete ? (
                  <div className="conv-confirm-row">
                    <span className="conv-confirm-text">Delete chat?</span>
                    <div className="conv-confirm-actions">
                      <button
                        type="button"
                        className="btn btn--danger-outline btn--xs"
                        disabled={isBusy}
                        onClick={() => {
                          void commitDelete(conv.conversation_id)
                        }}
                      >
                        Delete
                      </button>
                      <button
                        type="button"
                        className="btn btn--ghost btn--xs"
                        onClick={() => setConfirmDeleteId(null)}
                      >
                        Cancel
                      </button>
                    </div>
                  </div>
                ) : (
                  <>
                    <button
                      type="button"
                      className="conv-select-btn"
                      onClick={() => onSelectConversation(conv.conversation_id)}
                      title={conv.title}
                    >
                      <span className="conv-title">{conv.title}</span>
                      <span className="conv-meta">
                        {conv.message_count} msg
                        {conv.message_count === 1 ? '' : 's'}
                        {conv.task_count > 0
                          ? ` · ${conv.task_count} task${conv.task_count === 1 ? '' : 's'}`
                          : ''}
                      </span>
                    </button>
                    <div className="conv-item-actions">
                      <button
                        type="button"
                        className="icon-btn"
                        onClick={() => startEditing(conv)}
                        aria-label={`Rename ${conv.title}`}
                        title="Rename"
                      >
                        <EditIcon size={13} />
                      </button>
                      <button
                        type="button"
                        className="icon-btn icon-btn--danger"
                        onClick={() => {
                          setEditingId(null)
                          setConfirmDeleteId(conv.conversation_id)
                        }}
                        aria-label={`Delete ${conv.title}`}
                        title="Delete"
                      >
                        <TrashIcon size={13} />
                      </button>
                    </div>
                  </>
                )}
              </div>
            )
          })
        )}
      </div>

      <div className="sidebar-footer">
        <div className="backend-status-row">
          <span
            className={`conn-dot ${isHealthy ? 'conn-dot--ok' : backendStatus.state === 'error' ? 'conn-dot--err' : 'conn-dot--warn'}`}
            aria-hidden="true"
          />
          <div className="backend-status-text">
            <div className="backend-status-label">
              {backendConnectionLabel(backendStatus)}
            </div>
            <div className="backend-status-sub mono">
              {backendStatus.host && backendStatus.port
                ? `${backendStatus.host}:${backendStatus.port}`
                : backendStatus.lastError ?? 'Loopback sidecar'}
            </div>
          </div>
          <button
            type="button"
            className="icon-btn"
            onClick={onRestartBackend}
            disabled={restartingBackend}
            title="Restart local Python backend"
            aria-label="Restart local Python backend"
          >
            <RefreshIcon size={14} />
          </button>
        </div>
      </div>
    </aside>
  )
}
