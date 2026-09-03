import { Chip, severityRank } from '../lib/format'
import type { SeverityCounts } from '../lib/types'

export const SEVERITY_STYLES: Record<string, string> = {
  P0: 'bg-bad/15 text-bad border border-bad/30',
  P1: 'bg-warn/15 text-warn border border-warn/30',
  P2: 'bg-info/15 text-info border border-info/30',
  P3: 'bg-muted/15 text-muted border border-muted/30',
}

export const SEVERITY_DOT: Record<string, string> = {
  P0: 'bg-bad',
  P1: 'bg-warn',
  P2: 'bg-info',
  P3: 'bg-muted',
}

export function SeverityBadge({ severity }: { severity: string | null | undefined }) {
  const sev = (severity || '').toUpperCase()
  if (!sev) return <Chip className="bg-muted/10 text-muted">?</Chip>
  return <Chip className={SEVERITY_STYLES[sev] ?? 'bg-muted/10 text-muted'}>{sev}</Chip>
}

/** Compact "1 P0 · 2 P1" distribution for list rows; only levels present are shown. */
export function SeveritySummary({ counts }: { counts: SeverityCounts }) {
  const entries = Object.entries(counts ?? {})
    .filter(([sev, n]) => n > 0 && severityRank(sev) < 9)
    .sort(([a], [b]) => severityRank(a) - severityRank(b))
  if (entries.length === 0) {
    return <span className="text-xs text-muted">无发现</span>
  }
  return (
    <span className="inline-flex items-center gap-2">
      {entries.map(([sev, n]) => (
        <span key={sev} className="inline-flex items-center gap-1 text-xs tabular-nums">
          <span className={`h-1.5 w-1.5 rounded-full ${SEVERITY_DOT[sev] ?? 'bg-muted'}`} />
          <span className="text-muted">{n} {sev}</span>
        </span>
      ))}
    </span>
  )
}

const STATUS_STYLES = {
  RUNNING: 'bg-info/15 text-info border border-info/30',
  COMPLETED: 'bg-good/10 text-good border border-good/30',
  FAILED: 'bg-bad/15 text-bad border border-bad/30',
  SKIPPED: 'bg-muted/10 text-muted border border-muted/30',
} as const

export function StatusBadge({ status }: { status: 'RUNNING' | 'COMPLETED' | 'FAILED' | 'SKIPPED' | string }) {
  const style = STATUS_STYLES[status as keyof typeof STATUS_STYLES] ?? 'bg-muted/10 text-muted'
  return (
    <Chip className={style}>
      {status === 'RUNNING' && <span className="animate-breathe h-1.5 w-1.5 rounded-full bg-info" />}
      {status === 'COMPLETED' && <span className="h-1.5 w-1.5 rounded-full bg-good" />}
      {status === 'FAILED' && <span className="h-1.5 w-1.5 rounded-full bg-bad" />}
      {status === 'RUNNING' ? '进行中'
        : status === 'COMPLETED' ? '已完成'
        : status === 'SKIPPED' ? '已跳过'
        : '失败'}
    </Chip>
  )
}

const VERDICT_STYLES = {
  APPROVE: 'bg-good/10 text-good border border-good/30',
  REQUEST_CHANGES: 'bg-warn/15 text-warn border border-warn/30',
  COMMENT: 'bg-info/15 text-info border border-info/30',
} as const

const VERDICT_LABELS = {
  APPROVE: '✓ 通过',
  REQUEST_CHANGES: '✗ 需修改',
  COMMENT: '💬 评论',
} as const

export function VerdictBadge({ verdict }: { verdict: string | null | undefined }) {
  if (!verdict) return <span className="text-xs text-muted">—</span>
  const style = VERDICT_STYLES[verdict as keyof typeof VERDICT_STYLES] ?? 'bg-muted/10 text-muted'
  const label = VERDICT_LABELS[verdict as keyof typeof VERDICT_LABELS] ?? verdict
  return <Chip className={style}>{label}</Chip>
}

const TRIGGER_LABELS: Record<string, string> = {
  mention: '@ 触发',
  pr_open: 'PR 创建',
  push: 'Push',
  manual: '手动',
}

export function TriggerBadge({ trigger }: { trigger: string | null | undefined }) {
  return (
    <Chip className="bg-surface-3 text-muted">
      {TRIGGER_LABELS[trigger ?? ''] ?? trigger ?? '—'}
    </Chip>
  )
}

export function ComingSoonBadge({ phase }: { phase: string }) {
  return (
    <Chip className="border border-dashed border-muted/50 bg-muted/5 text-muted">
      待上线 · {phase}
    </Chip>
  )
}
