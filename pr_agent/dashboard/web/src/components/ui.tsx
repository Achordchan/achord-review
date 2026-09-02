import type { ReactNode } from 'react'

export function Card({ children, className = '', hover = false }: {
  children: ReactNode
  className?: string
  hover?: boolean
}) {
  return (
    <div
      className={`rounded-xl border border-line bg-surface-1 shadow-[0_1px_2px_rgba(0,0,0,0.25)] ${
        hover ? 'transition-all duration-200 hover:-translate-y-0.5 hover:border-accent/40 hover:shadow-[0_8px_24px_rgba(0,0,0,0.35)]' : ''
      } ${className}`}
    >
      {children}
    </div>
  )
}

export function CardHeader({ title, description, action }: {
  title: ReactNode
  description?: ReactNode
  action?: ReactNode
}) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-line px-5 py-4">
      <div>
        <h3 className="text-sm font-semibold text-text">{title}</h3>
        {description && <p className="mt-0.5 text-xs text-muted">{description}</p>}
      </div>
      {action}
    </div>
  )
}

export function StatCard({ label, value, sub, accent }: {
  label: string
  value: ReactNode
  sub?: ReactNode
  accent?: 'default' | 'good' | 'warn' | 'bad' | 'info'
}) {
  const accentClass =
    accent === 'good' ? 'text-good' :
    accent === 'warn' ? 'text-warn' :
    accent === 'bad' ? 'text-bad' :
    accent === 'info' ? 'text-info' : 'text-text'
  return (
    <Card hover className="p-5">
      <p className="text-xs font-medium uppercase tracking-wider text-muted">{label}</p>
      <p className={`mt-2 text-3xl font-semibold tabular-nums ${accentClass}`}>{value}</p>
      {sub && <p className="mt-1 text-xs text-muted">{sub}</p>}
    </Card>
  )
}

export function Skeleton({ className = '' }: { className?: string }) {
  return <div className={`skeleton ${className}`} />
}
