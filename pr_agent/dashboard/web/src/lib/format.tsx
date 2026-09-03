import type { ReactNode } from 'react'

export function formatDuration(ms: number): string {
  if (!ms || ms <= 0) return '—'
  if (ms < 1000) return `${ms}ms`
  const seconds = ms / 1000
  if (seconds < 60) return `${seconds.toFixed(1)}s`
  const roundedSeconds = Math.round(seconds)
  const minutes = Math.floor(roundedSeconds / 60)
  const rest = roundedSeconds % 60
  return `${minutes}m${rest > 0 ? `${rest}s` : ''}`
}

export function formatTokens(n: number): string {
  if (!n || n <= 0) return '0'
  if (n < 1000) return String(n)
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}k`
  return `${(n / 1_000_000).toFixed(2)}M`
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—'
  // storage timestamps are UTC "YYYY-MM-DD HH:MM:SS"; render them as UTC to stay
  // consistent with what the review pipeline wrote, without locale surprises
  const iso = value.includes('T') ? value : `${value.replace(' ', 'T')}Z`
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', { hour12: false })
}

export function relativeTime(value: string | null | undefined): string {
  if (!value) return '—'
  const iso = value.includes('T') ? value : `${value.replace(' ', 'T')}Z`
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return value
  const diff = Date.now() - then
  if (diff < 0) return '刚刚'
  const minutes = Math.floor(diff / 60_000)
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  const days = Math.floor(hours / 24)
  if (days < 14) return `${days} 天前`
  return formatDateTime(value)
}

export function shortSha(sha: string | null | undefined): string {
  return sha ? sha.slice(0, 7) : '—'
}

export function shortModel(model: string | null | undefined): string {
  if (!model) return '—'
  return model.replace(/^openai\//, '')
}

export function severityRank(sev: string | null | undefined): number {
  switch ((sev || '').toUpperCase()) {
    case 'P0': return 0
    case 'P1': return 1
    case 'P2': return 2
    case 'P3': return 3
    default: return 9
  }
}

export function countSeverity(counts: Record<string, number>, severity: string): number {
  return counts?.[severity] ?? 0
}

export function Chip({ children, className = '' }: { children: ReactNode; className?: string }) {
  return (
    <span className={`inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-xs font-medium ${className}`}>
      {children}
    </span>
  )
}
