export type InlineToken =
  | { type: 'text'; value: string }
  | { type: 'code'; value: string }
  | { type: 'strong'; children: InlineToken[] }
  | { type: 'em'; children: InlineToken[] }
  | { type: 'del'; children: InlineToken[] }
  | { type: 'link'; label: string; href: string }

export type MarkdownBlock =
  | { type: 'heading'; level: 1 | 2 | 3 | 4 | 5 | 6; tokens: InlineToken[] }
  | { type: 'paragraph'; tokens: InlineToken[] }
  | { type: 'code'; language: string; code: string }
  | { type: 'blockquote'; lines: InlineToken[][] }
  | { type: 'list'; ordered: boolean; items: InlineToken[][] }
  | { type: 'table'; headers: InlineToken[][]; rows: InlineToken[][][] }
  | { type: 'hr' }

function hasControlCharacters(value: string): boolean {
  for (let idx = 0; idx < value.length; idx += 1) {
    const code = value.charCodeAt(idx)
    if (code <= 0x1f || code === 0x7f) {
      return true
    }
  }
  return false
}

/**
 * Allows only safe http(s) URLs. Rejects javascript:, data:, file:, vbscript:,
 * protocol-relative URLs, and strings with control characters.
 */
export function sanitizeLinkUrl(rawUrl: string): string | null {
  const trimmed = rawUrl.trim()
  if (!trimmed || hasControlCharacters(trimmed)) {
    return null
  }
  try {
    const parsed = new URL(trimmed)
    if (parsed.protocol === 'https:' || parsed.protocol === 'http:') {
      return parsed.toString()
    }
    return null
  } catch {
    return null
  }
}

const INLINE_PATTERN_SOURCE =
  '(`[^`\\n]+`|\\*\\*[^*\\n]+?\\*\\*|~~[^~\\n]+?~~|\\*[^*\\n]+?\\*|\\[([^\\]\\n]+)\\]\\(([^)\\n]+)\\))'

export function parseInlineTokens(text: string, depth = 0): InlineToken[] {
  if (!text) {
    return []
  }
  if (depth > 3) {
    return [{ type: 'text', value: text }]
  }

  const pattern = new RegExp(INLINE_PATTERN_SOURCE, 'g')
  const tokens: InlineToken[] = []
  let lastIndex = 0
  let match: RegExpExecArray | null = pattern.exec(text)

  while (match !== null) {
    if (match.index > lastIndex) {
      tokens.push({ type: 'text', value: text.slice(lastIndex, match.index) })
    }

    const full = match[0]
    if (full.startsWith('`') && full.endsWith('`')) {
      tokens.push({ type: 'code', value: full.slice(1, -1) })
    } else if (full.startsWith('**') && full.endsWith('**')) {
      tokens.push({
        type: 'strong',
        children: parseInlineTokens(full.slice(2, -2), depth + 1),
      })
    } else if (full.startsWith('~~') && full.endsWith('~~')) {
      tokens.push({
        type: 'del',
        children: parseInlineTokens(full.slice(2, -2), depth + 1),
      })
    } else if (full.startsWith('*') && full.endsWith('*')) {
      tokens.push({
        type: 'em',
        children: parseInlineTokens(full.slice(1, -1), depth + 1),
      })
    } else if (full.startsWith('[')) {
      const label = match[2] ?? ''
      const rawHref = match[3] ?? ''
      const safeHref = sanitizeLinkUrl(rawHref)
      if (safeHref) {
        tokens.push({ type: 'link', label, href: safeHref })
      } else {
        // Unsafe URLs are never rendered as clickable links
        tokens.push({ type: 'text', value: label })
      }
    } else {
      tokens.push({ type: 'text', value: full })
    }

    lastIndex = match.index + full.length
    match = pattern.exec(text)
  }

  if (lastIndex < text.length) {
    tokens.push({ type: 'text', value: text.slice(lastIndex) })
  }

  return tokens
}

function isTableSeparator(line: string): boolean {
  const trimmed = line.trim()
  if (!trimmed.includes('|') || !trimmed.includes('-')) {
    return false
  }
  const cells = splitTableCells(trimmed)
  return (
    cells.length > 0 &&
    cells.every((cell) => /^:?-{3,}:?$/.test(cell.trim()))
  )
}

function splitTableCells(line: string): string[] {
  let trimmed = line.trim()
  if (trimmed.startsWith('|')) {
    trimmed = trimmed.slice(1)
  }
  if (trimmed.endsWith('|')) {
    trimmed = trimmed.slice(0, -1)
  }
  return trimmed.split('|').map((cell) => cell.trim())
}

function isBlockStart(line: string): boolean {
  const trimmed = line.trim()
  return (
    trimmed.startsWith('```') ||
    /^#{1,6}\s+/.test(trimmed) ||
    /^(-{3,}|\*{3,}|_{3,})$/.test(trimmed) ||
    trimmed.startsWith('> ') ||
    trimmed === '>' ||
    /^[-*+]\s+/.test(trimmed) ||
    /^\d+\.\s+/.test(trimmed)
  )
}

/**
 * Pure, zero-HTML Markdown block parser.
 * Never produces HTML strings; all text nodes are escaped by React when rendered.
 */
export function parseMarkdownBlocks(markdown: string): MarkdownBlock[] {
  const normalized = markdown.replace(/\r\n?/g, '\n')
  const lines = normalized.split('\n')
  const blocks: MarkdownBlock[] = []
  let i = 0

  while (i < lines.length) {
    const line = lines[i]
    const trimmed = line.trim()

    if (!trimmed) {
      i += 1
      continue
    }

    // Fenced code block
    if (trimmed.startsWith('```')) {
      const language = trimmed.slice(3).trim().split(/\s+/)[0] ?? ''
      const codeLines: string[] = []
      i += 1
      while (i < lines.length && !lines[i].trim().startsWith('```')) {
        codeLines.push(lines[i])
        i += 1
      }
      if (i < lines.length && lines[i].trim().startsWith('```')) {
        i += 1
      }
      blocks.push({
        type: 'code',
        language,
        code: codeLines.join('\n'),
      })
      continue
    }

    // Horizontal rule
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(trimmed)) {
      blocks.push({ type: 'hr' })
      i += 1
      continue
    }

    // Heading
    const headingMatch = /^(#{1,6})\s+(.*)$/.exec(trimmed)
    if (headingMatch) {
      const level = headingMatch[1].length as 1 | 2 | 3 | 4 | 5 | 6
      blocks.push({
        type: 'heading',
        level,
        tokens: parseInlineTokens(headingMatch[2].trim()),
      })
      i += 1
      continue
    }

    // Blockquote
    if (trimmed.startsWith('> ') || trimmed === '>') {
      const quoteLines: InlineToken[][] = []
      while (i < lines.length) {
        const cur = lines[i].trim()
        if (cur.startsWith('> ')) {
          quoteLines.push(parseInlineTokens(cur.slice(2)))
          i += 1
        } else if (cur === '>') {
          quoteLines.push([])
          i += 1
        } else {
          break
        }
      }
      blocks.push({ type: 'blockquote', lines: quoteLines })
      continue
    }

    // GFM Table
    if (
      trimmed.includes('|') &&
      i + 1 < lines.length &&
      isTableSeparator(lines[i + 1])
    ) {
      const headerCells = splitTableCells(trimmed)
      const headers = headerCells.map((cell) => parseInlineTokens(cell))
      i += 2
      const rows: InlineToken[][][] = []
      while (i < lines.length) {
        const rowTrimmed = lines[i].trim()
        if (!rowTrimmed || !rowTrimmed.includes('|')) {
          break
        }
        const cells = splitTableCells(rowTrimmed)
        const normalizedCells = headerCells.map((_, idx) =>
          parseInlineTokens(cells[idx] ?? ''),
        )
        rows.push(normalizedCells)
        i += 1
      }
      blocks.push({ type: 'table', headers, rows })
      continue
    }

    // Unordered list
    if (/^[-*+]\s+/.test(trimmed)) {
      const items: InlineToken[][] = []
      while (i < lines.length) {
        const match = /^\s*[-*+]\s+(.*)$/.exec(lines[i])
        if (!match) break
        items.push(parseInlineTokens(match[1]))
        i += 1
      }
      blocks.push({ type: 'list', ordered: false, items })
      continue
    }

    // Ordered list
    if (/^\d+\.\s+/.test(trimmed)) {
      const items: InlineToken[][] = []
      while (i < lines.length) {
        const match = /^\s*\d+\.\s+(.*)$/.exec(lines[i])
        if (!match) break
        items.push(parseInlineTokens(match[1]))
        i += 1
      }
      blocks.push({ type: 'list', ordered: true, items })
      continue
    }

    // Paragraph (coalesce contiguous non-block lines)
    const paraLines: string[] = [line.trim()]
    i += 1
    while (i < lines.length) {
      const nextTrimmed = lines[i].trim()
      if (!nextTrimmed || isBlockStart(lines[i])) {
        break
      }
      if (
        nextTrimmed.includes('|') &&
        i + 1 < lines.length &&
        isTableSeparator(lines[i + 1])
      ) {
        break
      }
      paraLines.push(nextTrimmed)
      i += 1
    }
    blocks.push({
      type: 'paragraph',
      tokens: parseInlineTokens(paraLines.join(' ')),
    })
  }

  return blocks
}
