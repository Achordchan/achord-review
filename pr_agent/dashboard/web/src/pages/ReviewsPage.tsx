import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { ExternalLink, RotateCcw, Search } from 'lucide-react'
import { api } from '../lib/api'
import type { ReviewListData, ReviewRow } from '../lib/types'
import { Card, Skeleton } from '../components/ui'
import { SeveritySummary, StatusBadge, TriggerBadge, VerdictBadge } from '../components/badges'
import { formatDuration, formatTokens, relativeTime, shortSha } from '../lib/format'
import { prHtmlUrl, repoHtmlUrl } from '../lib/github'

const STATUS_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'RUNNING', label: '进行中' },
  { value: 'COMPLETED', label: '已完成' },
  { value: 'SKIPPED', label: '已跳过' },
  { value: 'FAILED', label: '失败' },
]
const VERDICT_OPTIONS = [
  { value: '', label: '全部裁决' },
  { value: 'APPROVE', label: '通过' },
  { value: 'REQUEST_CHANGES', label: '需修改' },
  { value: 'COMMENT', label: '评论' },
]

export default function ReviewsPage() {
  const [repo, setRepo] = useState('')
  const [status, setStatus] = useState('')
  const [verdict, setVerdict] = useState('')
  const [page, setPage] = useState(0)
  const pageSize = 25

  const { data, isLoading, isError, refetch, isFetching } = useQuery({
    queryKey: ['reviews', repo, status, verdict, page],
    queryFn: () => {
      const params = new URLSearchParams()
      if (repo) params.set('repo', repo)
      if (status) params.set('status', status)
      if (verdict) params.set('verdict', verdict)
      params.set('limit', String(pageSize))
      params.set('offset', String(page * pageSize))
      return api.get<ReviewListData>(`/api/v1/dashboard/reviews?${params.toString()}`)
    },
    refetchInterval: (query) => {
      // poll while any visible row is still running
      const rows = query.state.data?.items ?? []
      return rows.some((r: ReviewRow) => r.status === 'RUNNING') ? 10_000 : 60_000
    },
  })

  const total = data?.total ?? 0
  const maxPage = Math.max(0, Math.ceil(total / pageSize) - 1)

  const resetPage = (setter: (v: string) => void) => (value: string) => {
    setter(value)
    setPage(0)
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold text-text">审查历史</h1>
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative">
            <Search size={14} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-muted" />
            <input
              value={repo}
              onChange={(e) => resetPage(setRepo)(e.target.value)}
              placeholder="按仓库名过滤…"
              className="w-52 rounded-lg border border-line bg-surface-1 py-1.5 pl-8 pr-3 text-xs text-text placeholder-muted/60 outline-none focus:border-accent"
            />
          </div>
          <select
            value={status}
            onChange={(e) => resetPage(setStatus)(e.target.value)}
            className="rounded-lg border border-line bg-surface-1 px-2.5 py-1.5 text-xs text-text outline-none focus:border-accent"
          >
            {STATUS_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
          <select
            value={verdict}
            onChange={(e) => resetPage(setVerdict)(e.target.value)}
            className="rounded-lg border border-line bg-surface-1 px-2.5 py-1.5 text-xs text-text outline-none focus:border-accent"
          >
            {VERDICT_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        </div>
      </div>

      {isLoading ? (
        <div className="space-y-2">
          {Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} className="h-16" />)}
        </div>
      ) : isError ? (
        <Card className="p-10 text-center">
          <p className="text-sm text-muted">加载失败</p>
          <button onClick={() => void refetch()} className="mt-3 rounded-lg border border-line px-4 py-2 text-sm text-text hover:bg-surface-2">
            重试
          </button>
        </Card>
      ) : !data || data.items.length === 0 ? (
        <Card className="flex flex-col items-center justify-center p-14">
          <div className="flex h-14 w-14 items-center justify-center rounded-full bg-surface-3 text-2xl">🔍</div>
          <p className="mt-4 text-sm font-medium text-text">暂无审查记录</p>
          <p className="mt-1 text-xs text-muted">调整过滤条件，或去 GitHub 上 @achord-review 触发第一次审查</p>
          <Link to="/dashboard/playground" className="mt-5 rounded-lg bg-accent-strong px-4 py-2 text-xs font-semibold text-white hover:bg-accent">
            去演练台手动触发 →
          </Link>
        </Card>
      ) : (
        <Card className="overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-line text-xs uppercase tracking-wider text-muted">
                  <th className="px-4 py-3 font-medium">PR</th>
                  <th className="px-4 py-3 font-medium">Commit</th>
                  <th className="px-4 py-3 font-medium">触发</th>
                  <th className="px-4 py-3 font-medium">发现</th>
                  <th className="px-4 py-3 font-medium">裁决</th>
                  <th className="px-4 py-3 font-medium">耗时</th>
                  <th className="px-4 py-3 font-medium">Token</th>
                  <th className="px-4 py-3 font-medium">状态</th>
                  <th className="px-4 py-3 font-medium">时间</th>
                </tr>
              </thead>
              <tbody className={isFetching ? 'opacity-70 transition-opacity' : 'transition-opacity'}>
                {data.items.map((row) => (
                  <tr key={row.id} className="group border-b border-line/60 transition-colors last:border-0 hover:bg-surface-2/60">
                    <td className="max-w-[260px] px-4 py-3">
                      <Link to={`/dashboard/reviews/${row.id}`} className="block truncate font-medium text-text hover:text-accent">
                        {row.pr_title || `PR #${row.pr_number}`}
                      </Link>
                      <div className="mt-0.5 flex items-center gap-1.5 text-xs text-muted">
                        <a
                          href={repoHtmlUrl(row.pr_url, row.repo_name)}
                          target="_blank"
                          rel="noreferrer"
                          className="truncate hover:text-accent"
                          title="打开仓库"
                        >
                          {row.repo_name}
                        </a>
                        <a
                          href={prHtmlUrl(row.pr_url, row.repo_name, row.pr_number)}
                          target="_blank"
                          rel="noreferrer"
                          className="inline-flex shrink-0 items-center gap-0.5 hover:text-accent"
                          title="在 GitHub 打开 PR"
                        >
                          #{row.pr_number}
                          <ExternalLink size={11} className="opacity-0 transition-opacity group-hover:opacity-100" />
                        </a>
                      </div>
                    </td>
                    <td className="px-4 py-3 font-mono text-xs text-muted">{shortSha(row.commit_sha)}</td>
                    <td className="px-4 py-3"><TriggerBadge trigger={row.trigger_type} /></td>
                    <td className="px-4 py-3"><SeveritySummary counts={row.severity_counts} /></td>
                    <td className="px-4 py-3"><VerdictBadge verdict={row.verdict} /></td>
                    <td className="px-4 py-3 text-xs tabular-nums text-muted">{formatDuration(row.duration_ms)}</td>
                    <td className="px-4 py-3 text-xs tabular-nums text-muted">{formatTokens(row.total_tokens)}</td>
                    <td className="px-4 py-3"><StatusBadge status={row.status} /></td>
                    <td className="px-4 py-3 text-xs text-muted" title={row.created_at}>{relativeTime(row.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="flex items-center justify-between border-t border-line px-4 py-3 text-xs text-muted">
            <span>共 {total} 条记录</span>
            <div className="flex items-center gap-2">
              <button
                onClick={() => setPage((p) => Math.max(0, p - 1))}
                disabled={page === 0}
                className="rounded-md border border-line px-2.5 py-1 transition-colors hover:bg-surface-2 disabled:opacity-40"
              >
                上一页
              </button>
              <span className="tabular-nums">{page + 1} / {maxPage + 1}</span>
              <button
                onClick={() => setPage((p) => Math.min(maxPage, p + 1))}
                disabled={page >= maxPage}
                className="rounded-md border border-line px-2.5 py-1 transition-colors hover:bg-surface-2 disabled:opacity-40"
              >
                下一页
              </button>
            </div>
          </div>
        </Card>
      )}
      <p className="flex items-center gap-1.5 text-xs text-muted">
        <RotateCcw size={11} />
        有进行中的审查时每 10 秒自动刷新，其余每 60 秒
      </p>
    </div>
  )
}
