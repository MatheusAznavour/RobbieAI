import type { SVGProps } from 'react'

type IconProps = SVGProps<SVGSVGElement> & {
  size?: number
}

function baseProps(size = 16, rest: SVGProps<SVGSVGElement> = {}) {
  return {
    width: size,
    height: size,
    viewBox: '0 0 24 24',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 1.75,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
    'aria-hidden': true,
    ...rest,
  }
}

export function RobbieEmblem({ size = 24, ...rest }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 28 28"
      fill="none"
      aria-hidden="true"
      {...rest}
    >
      <rect
        x="2"
        y="2"
        width="24"
        height="24"
        rx="7"
        fill="url(#robbie-emblem-bg)"
        stroke="rgba(255,255,255,0.14)"
        strokeWidth="1"
      />
      <path
        d="M9.5 18.5V9.5H14.6C16.5 9.5 17.8 10.6 17.8 12.3C17.8 13.7 16.9 14.7 15.5 15L18.2 18.5H15.6L13.2 15.2H11.6V18.5H9.5ZM11.6 13.4H14.3C15.2 13.4 15.7 13 15.7 12.3C15.7 11.6 15.2 11.2 14.3 11.2H11.6V13.4Z"
        fill="#F8FAFC"
      />
      <circle cx="20.2" cy="8.2" r="2" fill="#38BDF8" />
      <defs>
        <linearGradient
          id="robbie-emblem-bg"
          x1="2"
          y1="2"
          x2="26"
          y2="26"
          gradientUnits="userSpaceOnUse"
        >
          <stop stopColor="#1E293B" />
          <stop offset="1" stopColor="#0F172A" />
        </linearGradient>
      </defs>
    </svg>
  )
}

export function PlusIcon({ size = 16, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <path d="M12 5v14M5 12h14" />
    </svg>
  )
}

export function SearchIcon({ size = 15, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <circle cx="11" cy="11" r="7" />
      <path d="m20 20-3.5-3.5" />
    </svg>
  )
}

export function ChatIcon({ size = 16, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <path d="M21 12a8.5 8.5 0 0 1-8.5 8.5c-1.4 0-2.7-.3-3.9-.9L3 21l1.4-5.1A8.5 8.5 0 1 1 21 12Z" />
    </svg>
  )
}

export function TasksIcon({ size = 16, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <rect x="3" y="4" width="18" height="14" rx="2" />
      <path d="M8 20h8M12 18v2M7.5 11l2.5 2 4.5-4.5" />
    </svg>
  )
}

export function ShieldIcon({ size = 16, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <path d="M12 3 5 6v6c0 4.5 3 7.7 7 9 4-1.3 7-4.5 7-9V6l-7-3Z" />
    </svg>
  )
}

export function SettingsIcon({ size = 16, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3h.1a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8v.1a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z" />
    </svg>
  )
}

export function StopOctagonIcon({ size = 16, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <polygon points="7.86 2 16.14 2 22 7.86 22 16.14 16.14 22 7.86 22 2 16.14 2 7.86 7.86 2" />
      <rect x="9" y="9" width="6" height="6" fill="currentColor" stroke="none" />
    </svg>
  )
}

export function SendIcon({ size = 16, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <path d="M12 19V5M5 12l7-7 7 7" />
    </svg>
  )
}

export function CopyIcon({ size = 14, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <rect x="9" y="9" width="12" height="12" rx="2" />
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
    </svg>
  )
}

export function CheckIcon({ size = 14, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <path d="m5 12 4.5 4.5L19 7" />
    </svg>
  )
}

export function RefreshIcon({ size = 14, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <path d="M21 12a9 9 0 0 1-15.4 6.4L3 16" />
      <path d="M3 12A9 9 0 0 1 18.4 5.6L21 8" />
      <path d="M21 3v5h-5M3 21v-5h5" />
    </svg>
  )
}

export function EditIcon({ size = 14, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5Z" />
    </svg>
  )
}

export function TrashIcon({ size = 14, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
    </svg>
  )
}

export function PauseIcon({ size = 14, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <rect x="6" y="5" width="4" height="14" rx="1" />
      <rect x="14" y="5" width="4" height="14" rx="1" />
    </svg>
  )
}

export function PlayIcon({ size = 14, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <polygon points="6 4 20 12 6 20 6 4" />
    </svg>
  )
}

export function XIcon({ size = 14, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <path d="M18 6 6 18M6 6l12 12" />
    </svg>
  )
}

export function DesktopIcon({ size = 15, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <rect x="2" y="3" width="20" height="14" rx="2" />
      <path d="M8 21h8M12 17v4" />
    </svg>
  )
}

export function AlertIcon({ size = 15, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <path d="M10.3 3.9 1.8 18.2A2 2 0 0 0 3.5 21h17a2 2 0 0 0 1.7-2.8L13.7 3.9a2 2 0 0 0-3.4 0Z" />
      <path d="M12 9v4M12 17h.01" />
    </svg>
  )
}

export function LockIcon({ size = 14, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <rect x="5" y="11" width="14" height="10" rx="2" />
      <path d="M8 11V7a4 4 0 0 1 8 0v4" />
    </svg>
  )
}
