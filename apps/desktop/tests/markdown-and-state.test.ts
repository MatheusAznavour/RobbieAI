import assert from 'node:assert/strict'
import test from 'node:test'
import {
  parseInlineTokens,
  parseMarkdownBlocks,
  sanitizeLinkUrl,
} from '../src/lib/markdown.ts'
import {
  appendMessageDeduplicated,
  applyTaskEvent,
  collectPendingApprovals,
  isConflictError,
  mergeTaskEvents,
  updateDraftMap,
  upsertConversationSummary,
  upsertTaskRecord,
} from '../src/lib/stateReconciler.ts'
import type {
  ChatMessage,
  ConversationSummary,
  PendingApproval,
  TaskEventItem,
  TaskRecordView,
} from '../shared/backend.ts'

test('sanitizeLinkUrl allows only http and https URLs and blocks dangerous schemes', () => {
  assert.equal(sanitizeLinkUrl('https://example.com/docs'), 'https://example.com/docs')
  assert.equal(sanitizeLinkUrl('http://127.0.0.1:8080/test'), 'http://127.0.0.1:8080/test')
  assert.equal(sanitizeLinkUrl('javascript:alert(1)'), null)
  assert.equal(sanitizeLinkUrl('JAVASCRIPT:alert(1)'), null)
  assert.equal(sanitizeLinkUrl('data:text/html,<script>alert(1)</script>'), null)
  assert.equal(sanitizeLinkUrl('vbscript:msgbox(1)'), null)
  assert.equal(sanitizeLinkUrl('file:///C:/Windows/System32/cmd.exe'), null)
  assert.equal(sanitizeLinkUrl(''), null)
})

test('Markdown parser keeps raw HTML inert and strips unsafe link protocols', () => {
  const xssInput = [
    '# Heading',
    '',
    'Hello <script>alert("xss")</script> and <img src=x onerror=alert(1)>',
    '',
    'Click [evil](javascript:alert(1)) or [safe](https://example.com/guide).',
    '',
    '```python',
    'print("<script>not html</script>")',
    '```',
  ].join('\n')

  const blocks = parseMarkdownBlocks(xssInput)
  assert.equal(blocks[0].type, 'heading')
  assert.equal(blocks[1].type, 'paragraph')
  if (blocks[1].type === 'paragraph') {
    assert.equal(blocks[1].tokens.length, 1)
    assert.equal(blocks[1].tokens[0].type, 'text')
    assert.ok(blocks[1].tokens[0].value.includes('<script>alert("xss")</script>'))
  }

  assert.equal(blocks[2].type, 'paragraph')
  if (blocks[2].type === 'paragraph') {
    const links = blocks[2].tokens.filter((t) => t.type === 'link')
    assert.equal(links.length, 1)
    assert.equal(links[0].type, 'link')
    if (links[0].type === 'link') {
      assert.equal(links[0].href, 'https://example.com/guide')
      assert.equal(links[0].label, 'safe')
    }
  }

  assert.equal(blocks[3].type, 'code')
  if (blocks[3].type === 'code') {
    assert.equal(blocks[3].language, 'python')
    assert.equal(blocks[3].code, 'print("<script>not html</script>")')
  }
})

test('Markdown parser supports lists, blockquotes, tables, and inline emphasis', () => {
  const md = [
    '> Safety first',
    '> Always verify',
    '',
    '- Item **one**',
    '- Item `two`',
    '',
    '1. First',
    '2. Second',
    '',
    '| Action | Risk |',
    '| --- | --- |',
    '| Click | Low |',
    '| Delete | High |',
  ].join('\n')

  const blocks = parseMarkdownBlocks(md)
  assert.equal(blocks.length, 4)
  assert.equal(blocks[0].type, 'blockquote')
  assert.equal(blocks[1].type, 'list')
  if (blocks[1].type === 'list') {
    assert.equal(blocks[1].ordered, false)
    assert.equal(blocks[1].items.length, 2)
  }
  assert.equal(blocks[2].type, 'list')
  if (blocks[2].type === 'list') {
    assert.equal(blocks[2].ordered, true)
    assert.equal(blocks[2].items.length, 2)
  }
  assert.equal(blocks[3].type, 'table')
  if (blocks[3].type === 'table') {
    assert.equal(blocks[3].headers.length, 2)
    assert.equal(blocks[3].rows.length, 2)
  }

  const inline = parseInlineTokens('**bold** *italic* ~~strike~~ `code`')
  assert.ok(inline.some((t) => t.type === 'strong'))
  assert.ok(inline.some((t) => t.type === 'em'))
  assert.ok(inline.some((t) => t.type === 'del'))
  assert.ok(inline.some((t) => t.type === 'code'))
})

test('stateReconciler deduplicates messages, events, and protects terminal task states from out-of-order events', () => {
  const msg1: ChatMessage = {
    message_id: 'm1',
    conversation_id: 'c1',
    role: 'user',
    content: 'Hi',
    kind: 'chat',
    task_id: null,
    created_at: '2026-11-26T00:00:00.000Z',
  }
  const appended = appendMessageDeduplicated([msg1], msg1)
  assert.equal(appended.length, 1)

  const approval: PendingApproval = {
    approval_id: 'ap-1',
    task_id: 't1',
    step_id: 's1',
    action_digest: 'b'.repeat(64),
    action: { kind: 'type_text', text: '[REDACTED]', text_length: 12 },
    risk_level: 'high',
    category: 'sensitive_task_context',
    reasons: ['Sensitive context'],
    created_at: '2026-11-26T00:00:02.000Z',
  }

  const baseTask: TaskRecordView = {
    task_id: 't1',
    conversation_id: 'c1',
    instruction: 'Type message',
    state: 'running',
    current_step: 1,
    max_steps: 10,
    last_step_id: 's1',
    last_error: null,
    created_at: '2026-11-26T00:00:00.000Z',
    updated_at: '2026-11-26T00:00:01.000Z',
    pending_approval: null,
    events: [],
  }

  const approvalEvent: TaskEventItem = {
    type: 'approval_required',
    timestamp: '2026-11-26T00:00:02.000Z',
    task_id: 't1',
    step_id: 's1',
    approval,
  }

  // Applying the same WS event twice must not duplicate timeline entries
  let tasks = applyTaskEvent([baseTask], approvalEvent)
  tasks = applyTaskEvent(tasks, approvalEvent)
  assert.equal(tasks[0].state, 'awaiting_approval')
  assert.equal(tasks[0].events.length, 1)
  assert.equal(collectPendingApprovals(tasks).length, 1)

  // Emergency stop transitions task to terminal state and clears pending approval
  tasks = applyTaskEvent(tasks, {
    type: 'task_emergency_stopped',
    timestamp: '2026-11-26T00:00:03.000Z',
    task_id: 't1',
  })
  assert.equal(tasks[0].state, 'emergency_stopped')
  assert.equal(tasks[0].pending_approval, null)
  assert.equal(collectPendingApprovals(tasks).length, 0)

  // Out-of-order older non-terminal event must NOT resurrect emergency_stopped task
  tasks = applyTaskEvent(tasks, {
    type: 'task_resumed',
    timestamp: '2026-11-26T00:00:02.500Z',
    task_id: 't1',
  })
  assert.equal(tasks[0].state, 'emergency_stopped')
  assert.equal(tasks[0].pending_approval, null)

  // Upsert preserves merged deduplicated events
  const merged = upsertTaskRecord(tasks, { ...tasks[0], events: [approvalEvent] })
  assert.equal(
    mergeTaskEvents(merged[0].events, [approvalEvent]).length,
    merged[0].events.length,
  )
})

test('stateReconciler tracks drafts, sorts conversations, and detects HTTP 409 conflicts', () => {
  let drafts: Record<string, string> = {}
  drafts = updateDraftMap(drafts, 'conv-a', 'Draft for A')
  drafts = updateDraftMap(drafts, 'conv-b', 'Draft for B')
  assert.equal(drafts['conv-a'], 'Draft for A')
  assert.equal(drafts['conv-b'], 'Draft for B')
  drafts = updateDraftMap(drafts, 'conv-a', '')
  assert.equal('conv-a' in drafts, false)

  const c1: ConversationSummary = {
    conversation_id: 'c1',
    title: 'Older',
    created_at: '2026-11-26T00:00:00.000Z',
    updated_at: '2026-11-26T00:00:01.000Z',
    message_count: 1,
    task_count: 0,
    last_message_preview: '',
  }
  const c2: ConversationSummary = {
    conversation_id: 'c2',
    title: 'Newer',
    created_at: '2026-11-26T00:00:02.000Z',
    updated_at: '2026-11-26T00:00:05.000Z',
    message_count: 2,
    task_count: 1,
    last_message_preview: 'Hi',
  }
  const sorted = upsertConversationSummary([c1], c2)
  assert.equal(sorted[0].conversation_id, 'c2')
  assert.equal(sorted[1].conversation_id, 'c1')

  assert.equal(isConflictError(new Error('HTTP 409: Approval already resolved')), true)
  assert.equal(isConflictError(new Error('HTTP 500: Internal error')), false)
})

test('stateReconciler reconciles backend step_started, step_planned, and action_rejected events', () => {
  const baseTask: TaskRecordView = {
    task_id: 't-parity',
    conversation_id: 'c1',
    instruction: 'Open Firefox',
    state: 'pending',
    current_step: 0,
    max_steps: 15,
    last_step_id: null,
    last_error: null,
    created_at: '2026-11-26T00:00:00.000Z',
    updated_at: '2026-11-26T00:00:00.000Z',
    pending_approval: null,
    events: [],
  }

  let tasks = applyTaskEvent([baseTask], {
    type: 'step_started',
    timestamp: '2026-11-26T00:00:01.000Z',
    task_id: 't-parity',
    step_id: 't-parity:step-1',
    step_index: 1,
  })
  assert.equal(tasks[0].state, 'running')
  assert.equal(tasks[0].current_step, 1)
  assert.equal(tasks[0].last_step_id, 't-parity:step-1')

  tasks = applyTaskEvent(tasks, {
    type: 'step_planned',
    timestamp: '2026-11-26T00:00:02.000Z',
    task_id: 't-parity',
    step_id: 't-parity:step-1',
    action: { kind: 'open', target: 'Firefox' },
    risk_level: 'high',
    category: 'non_allowlisted_open',
    requires_approval: true,
  })
  assert.equal(tasks[0].events.length, 2)
  assert.equal(tasks[0].events[1].type, 'step_planned')

  tasks = applyTaskEvent(tasks, {
    type: 'action_rejected',
    timestamp: '2026-11-26T00:00:03.000Z',
    task_id: 't-parity',
    step_id: 't-parity:step-1',
    action: { kind: 'open', target: 'Firefox' },
    reason: 'User denied action.',
  })
  assert.equal(tasks[0].events.length, 3)
  assert.equal(tasks[0].events[2].type, 'action_rejected')
})
