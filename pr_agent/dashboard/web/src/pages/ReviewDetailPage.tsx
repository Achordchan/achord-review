import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { ArrowLeft, Ban, ChevronDown, ChevronUp, ExternalLink } from 'lucide-react'
import { prHtmlUrl, repoHtmlUrl } from '../lib/github'
import { api, ApiError } from '../lib/api'
import type { ReviewDetail } from '../lib/types'
import { Card, CardHeader, Skeleton } from '../components/ui'
import { ConfirmDialog } from '../components/Dialogs'
import { useToast } from '../components/Toast'
import { SeverityBadge, StatusBadge, TriggerBadge, VerdictBadge } from '../components/badges'
import { MarkdownView } from '../components/MarkdownView'
import { formatDuration, formatTokens, formatDateTime, shortModel, shortSha } from '../lib/format'

function MetaItem({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <p className="text-[11px] uppercase tracking-wider text-muted">{label}</p>
      <p className="mt-1 truncate text-sm text-text">{children}</p>
    </div>
  )
}

function Collapsible({ title, children, defaultOpen = false }: {
  title: string
  children: React.ReactNode
  defaultOpen?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <Card>
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between px-5 py-4 text-left"
      >
        <span className="text-sm font-semibold text-text">{title}</span>
        {open ? <ChevronUp size={16} className="text-muted" /> : <ChevronDown size={16} className="text-muted" />}
      </button>
      {open && <div className="border-t border-line px-5 py-4">{children}</div>}
    </Card>
  )
}

export default function ReviewDetailPage() {
  const { id } = useParams<{ id: string }>()
  const reviewId = Number(id)
  const toast = useToast()
  const queryClient = useQueryClient()
  const [confirmStop, setConfirmStop] = useState(false)
  const [stopping, setStopping] = useState(false)
  const { data, isLoading, isError } = useQuery({
    queryKey: ['review-detail', reviewId],
    queryFn: () => api.get<ReviewDetail>(`/api/v1/dashboard/reviews/${reviewId}`),
    enabled: Number.isInteger(reviewId) && reviewId > 0,
    refetchInterval: (query) => (query.state.data?.status === 'RUNNING' ? 8_000 : false),
  })

  const stopReview = async () => {
    if (stopping) return
    setStopping(true)
    try {
      const body = await api.post<{ cancel_requested: boolean }>(
        `/api/v1/dashboard/reviews/${reviewId}/cancel`)
      if (body.cancel_requested) {
        toast.success('已请求停止', '将在下一次心跳时生效，最长约 1 分钟')
      } else {
        toast.info('无需停止', '该审查已结束或未在运行')
      }
      await queryClient.invalidateQueries({ queryKey: ['review-detail', reviewId] })
    } catch (err) {
      toast.error('停止失败', err instanceof ApiError ? err.message : '未知错误')
    } finally {
      setStopping(false)
      setConfirmStop(false)
    }
  }

  if (Number.isNaN(reviewId) || reviewId <= 0) {
    return (
      <Card className="p-10 text-center text-sm text-muted">
        无效的记录 ID。<Link to="/dashboard/reviews" className="text-accent hover:underline">返回审查历史</Link>
      </Card>
    )
  }
  if (isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-28" />
        <Skeleton className="h-64" />
      </div>
    )
  }
  if (isError || !data) {
    return (
      <Card className="p-10 text-center text-sm text-muted">
        审查记录不存在或已删除。<Link to="/dashboard/reviews" className="text-accent hover:underline">返回列表</Link>
      </Card>
    )
  }

  const issues = [...(data.issues ?? [])]

  // The deep-link target is GitHub's own review html_url when we captured it;
  // fall back to the PR page. Guard the scheme so only http(s) ever reaches the
  // href, never a javascript:/data: URL from an unexpected stored value.
  const reviewUrl = /^https?:\/\//i.test(data.review_comment_url ?? '') ? data.review_comment_url! : ''
  const openUrl = reviewUrl || prHtmlUrl(data.pr_url, data.repo_name, data.pr_number)

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Link
            to="/dashboard/reviews"
            className="flex h-8 w-8 items-center justify-center rounded-lg border border-line text-muted transition-colors hover:bg-surface-2 hover:text-text"
            title="返回列表"
          >
            <ArrowLeft size={15} />
          </Link>
          <h1 className="max-w-2xl truncate text-xl font-semibold text-text">
            {data.pr_title || `PR #${data.pr_number}`}
          </h1>
        </div>
        <div className="flex items-center gap-2">
          {data.status === 'RUNNING' && (
            <button
              onClick={() => setConfirmStop(true)}
              disabled={stopping}
              title="手动停止这条卡住的审查"
              className="flex items-center gap-1.5 rounded-lg border border-bad/40 px-3 py-1.5 text-xs font-medium text-bad transition-colors hover:bg-bad/10 disabled:opacity-40"
            >
              {stopping ? <span className="h-3 w-3 animate-spin rounded-full border-2 border-bad/40 border-t-bad" /> : <Ban size={12} />}
              {stopping ? '停止中…' : '停止审查'}
            </button>
          )}
          <a
            href={openUrl}
            target="_blank"
            rel="noreferrer"
            title={reviewUrl ? '跳转到本次审查评论' : '跳转到 PR 页面'}
            className="flex items-center gap-1.5 rounded-lg border border-line px-3 py-1.5 text-xs text-muted transition-colors hover:bg-surface-2 hover:text-text"
          >
            在 GitHub 打开 <ExternalLink size={12} />
          </a>
        </div>
      </div>

      <Card className="p-5">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge status={data.status} />
          <VerdictBadge verdict={data.verdict} />
          <TriggerBadge trigger={data.trigger_type} />
          <span className="rounded-md bg-surface-3 px-1.5 py-0.5 text-xs text-muted">{data.command}</span>
        </div>
        <div className="mt-4 grid grid-cols-2 gap-x-6 gap-y-3 md:grid-cols-4">
          <MetaItem label="仓库"><a href={repoHtmlUrl(data.pr_url, data.repo_name)} target="_blank" rel="noreferrer" className="hover:text-accent">{data.repo_name}</a></MetaItem>
          <MetaItem label="PR 编号">#{data.pr_number}</MetaItem>
          <MetaItem label="Commit">{shortSha(data.commit_sha)}</MetaItem>
          <MetaItem label="触发者">{data.sender || '—'}</MetaItem>
          <MetaItem label="模型">{shortModel(data.model)}{data.reasoning_effort ? ` · ${data.reasoning_effort}` : ''}</MetaItem>
          <MetaItem label="总耗时">{formatDuration(data.duration_ms)}</MetaItem>
          <MetaItem label="Token">
            {formatTokens(data.total_tokens)}
            <span className="ml-1 text-xs text-muted">({formatTokens(data.prompt_tokens)}+{formatTokens(data.completion_tokens)})</span>
          </MetaItem>
          <MetaItem label="触发时间">{formatDateTime(data.created_at)}</MetaItem>
        </div>
        {data.verdict_reason && (
          <p className="mt-4 rounded-lg border border-line bg-surface-2/60 px-4 py-3 text-sm leading-relaxed text-muted">
            <span className="font-medium text-text">裁决理由：</span>{data.verdict_reason}
          </p>
        )}
        {(data.status === 'FAILED' || data.status === 'SKIPPED') && data.error_message && (
          <p className={`mt-4 rounded-lg border px-4 py-3 text-xs leading-relaxed ${
            data.status === 'FAILED'
              ? 'border-bad/30 bg-bad/10 font-mono text-bad'
              : 'border-line bg-surface-2/60 text-muted'
          }`}>
            {data.status === 'SKIPPED' && <span className="font-medium text-text">跳过原因：</span>}
            {data.error_message}
          </p>
        )}
      </Card>

      {data.markdown_output && (
        <Card>
          <CardHeader title="审查报告" description="发布到 GitHub 的同款 Markdown 内容" />
          <div className="px-5 py-4">
            <MarkdownView content={data.markdown_output} />
          </div>
        </Card>
      )}

      <Card>
        <CardHeader
          title={`Findings（${issues.length}）`}
          description="按文件与严重度列出本次审查发现的全部问题"
        />
        {issues.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-10">
            <p className="text-sm text-muted">
              {data.status === 'RUNNING'
                ? '审查进行中，Findings 将在完成后显示'
                : data.status === 'SKIPPED'
                  ? '本次审查已跳过，未生成 Findings'
                  : data.status === 'FAILED'
                    ? '本次审查失败，未生成 Findings'
                    : '本次审查没有发现问题'}
            </p>
          </div>
        ) : (
          <ul className="divide-y divide-line">
            {issues.map((issue) => (
              <li key={issue.id} className="px-5 py-4 transition-colors hover:bg-surface-2/40">
                <div className="flex flex-wrap items-center gap-2.5">
                  <SeverityBadge severity={issue.severity} />
                  <span className="font-mono text-xs text-info">
                    {issue.relevant_file || '未知文件'}
                    {issue.relevant_lines_start ? `:${issue.relevant_lines_start}` : ''}
                    {issue.relevant_lines_end && issue.relevant_lines_end !== issue.relevant_lines_start ? `-${issue.relevant_lines_end}` : ''}
                  </span>
                </div>
                <p className="mt-1.5 text-sm font-medium text-text">{issue.issue_summary}</p>
                {issue.suggestion && (
                  <p className="mt-1 text-xs leading-relaxed text-muted">{issue.suggestion}</p>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>

      {data.raw_prediction && (
        <Collapsible title="原始 AI 输出（Raw Prediction）">
          <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-line bg-surface-2/60 p-4 font-mono text-xs leading-relaxed text-muted">
            {data.raw_prediction}
          </pre>
        </Collapsible>
      )}

      <ConfirmDialog
        open={confirmStop}
        title="停止这条审查？"
        body="将请求评审进程尽快中止，该记录会标记为失败（原因：管理员手动停止）。生效有约一次心跳的延迟（最长约 1 分钟）。之后可对该 PR 重新触发审查。"
        danger
        confirmLabel="停止审查"
        onConfirm={() => void stopReview()}
        onCancel={() => setConfirmStop(false)}
      />
    </div>
  )
}
