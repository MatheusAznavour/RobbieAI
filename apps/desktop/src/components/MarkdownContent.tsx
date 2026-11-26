import { useMemo, useState, type ReactNode } from 'react'
import {
  parseMarkdownBlocks,
  type InlineToken,
  type MarkdownBlock,
} from '../lib/markdown.ts'
import { CheckIcon, CopyIcon } from './Icons.tsx'

function renderInlineTokens(tokens: InlineToken[], keyPrefix: string): ReactNode[] {
  return tokens.map((token, idx) => {
    const key = `${keyPrefix}-${idx}`
    switch (token.type) {
      case 'text':
        return <span key={key}>{token.value}</span>
      case 'code':
        return (
          <code key={key} className="md-inline-code">
            {token.value}
          </code>
        )
      case 'strong':
        return (
          <strong key={key}>
            {renderInlineTokens(token.children, `${key}-b`)}
          </strong>
        )
      case 'em':
        return (
          <em key={key}>{renderInlineTokens(token.children, `${key}-i`)}</em>
        )
      case 'del':
        return (
          <del key={key}>{renderInlineTokens(token.children, `${key}-d`)}</del>
        )
      case 'link':
        return (
          <a
            key={key}
            href={token.href}
            target="_blank"
            rel="noopener noreferrer"
            className="md-link"
          >
            {token.label}
          </a>
        )
    }
  })
}

function CodeBlock({ language, code }: { language: string; code: string }) {
  const [copied, setCopied] = useState(false)

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(code)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1800)
    } catch {
      setCopied(false)
    }
  }

  return (
    <div className="md-code-block">
      <div className="md-code-header">
        <span className="md-code-lang">{language || 'text'}</span>
        <button
          type="button"
          className="md-code-copy"
          onClick={() => {
            void handleCopy()
          }}
          aria-label="Copy code block"
        >
          {copied ? <CheckIcon size={13} /> : <CopyIcon size={13} />}
          <span>{copied ? 'Copied' : 'Copy'}</span>
        </button>
      </div>
      <pre className="md-code-pre">
        <code>{code}</code>
      </pre>
    </div>
  )
}

function renderBlock(block: MarkdownBlock, idx: number): ReactNode {
  const key = `b-${idx}`
  switch (block.type) {
    case 'heading': {
      const content = renderInlineTokens(block.tokens, key)
      const Tag = `h${Math.min(block.level + 1, 6)}` as
        | 'h2'
        | 'h3'
        | 'h4'
        | 'h5'
        | 'h6'
      return (
        <Tag key={key} className={`md-heading md-h${block.level}`}>
          {content}
        </Tag>
      )
    }
    case 'paragraph':
      return (
        <p key={key} className="md-p">
          {renderInlineTokens(block.tokens, key)}
        </p>
      )
    case 'code':
      return <CodeBlock key={key} language={block.language} code={block.code} />
    case 'blockquote':
      return (
        <blockquote key={key} className="md-blockquote">
          {block.lines.map((line, lineIdx) => (
            <p key={`${key}-q-${lineIdx}`} className="md-p">
              {renderInlineTokens(line, `${key}-q-${lineIdx}`)}
            </p>
          ))}
        </blockquote>
      )
    case 'list': {
      const ListTag = block.ordered ? 'ol' : 'ul'
      return (
        <ListTag key={key} className={block.ordered ? 'md-ol' : 'md-ul'}>
          {block.items.map((item, itemIdx) => (
            <li key={`${key}-li-${itemIdx}`}>
              {renderInlineTokens(item, `${key}-li-${itemIdx}`)}
            </li>
          ))}
        </ListTag>
      )
    }
    case 'table':
      return (
        <div key={key} className="md-table-wrap">
          <table className="md-table">
            <thead>
              <tr>
                {block.headers.map((header, hIdx) => (
                  <th key={`${key}-th-${hIdx}`}>
                    {renderInlineTokens(header, `${key}-th-${hIdx}`)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, rIdx) => (
                <tr key={`${key}-tr-${rIdx}`}>
                  {row.map((cell, cIdx) => (
                    <td key={`${key}-td-${rIdx}-${cIdx}`}>
                      {renderInlineTokens(cell, `${key}-td-${rIdx}-${cIdx}`)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )
    case 'hr':
      return <hr key={key} className="md-hr" />
  }
}

export function MarkdownContent({ content }: { content: string }) {
  const blocks = useMemo(() => parseMarkdownBlocks(content), [content])

  return <div className="md-content">{blocks.map(renderBlock)}</div>
}
