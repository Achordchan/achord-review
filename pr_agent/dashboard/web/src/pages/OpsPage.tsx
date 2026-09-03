import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Activity, GitPullRequestArrow, HeartPulse, RefreshCcw, Terminal } from 'lucide-react'
import { api, ApiError } from '../lib/api'
import type { AuditLogListData, DiagnoseResult, OpsCapabilities, OpsResult } from '../lib/types'
import { Card, CardHeader, Skeleton } from '../components/ui'
import { ConfirmDialog } from '../components/Dialogs'
import { useToast } from '../components/Toast'
import { formatDateTime } from '../lib/format'

function isOpsResult(value: unknown): value is OpsResult {
  if (!value || typeof value !== 'object') return false
  const result = value as Partial<OpsResult>
  return typeof result.started === 'boolean'
    && typeof result.completed === 'boolean'
    && (result.exit_code === null || typeof result.exit_code === 'number')
    && Array.isArray(result.output)
    && result.output.every((line) => typeof line === 'string')
}

function ProbeRow({ name, result, loading }: { name: string; result?: Record<string, unknown>; loading?: boolean }) {
  const ok = result?.ok
  return (
    <div className="flex items-center justify-between border-b border-line/60 px-5 py-3 last:border-0">
      <span className="text-sm text-text">{name}</span>
      {loading ? (
        <span className="text-xs text-muted">检测中…</span>
      ) : ok ? (
        <span className="flex items-center gap-1.5 text-xs font-medium text-good">
          <span className="h-1.5 w-1.5 rounded-full bg-good" /> 正常
          {typeof result?.latency_ms === 'number' && <span className="text-muted">· {result.latency_ms}ms</span>}
          {typeof result?.app_name === 'string' && <span className="text-muted">· {result.app_name}</span>}
        </span>
      ) : (
        <span className="max-w-[60%] truncate text-xs text-bad" title={String(result?.error ?? '')}>
          {String(result?.error ?? '未知错误')}
        </span>
      )}
    </div>
  )
}

export default function OpsPage() {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [confirmAction, setConfirmAction] = useState<'restart' | 'pull' | null>(null)
  const [task, setTask] = useState<OpsResult | null>(null)
  const [diagnose, setDiagnose] = useState<DiagnoseResult | null>(null)
  const [diagnosing, setDiagnosing] = useState(false)

  const logsQuery = useQuery({
    queryKey: ['ops-logs'],
    queryFn: () => api.get<{ lines: string[] }>('/api/v1/dashboard/ops/logs'),
    refetchInterval: 5_000,
  })

  const auditQuery = useQuery({
    queryKey: ['audit-logs'],
    queryFn: () => api.get<AuditLogListData>('/api/v1/dashboard/audit-logs?limit=50'),
    refetchInterval: 30_000,
  })

  const capabilitiesQuery = useQuery({
    queryKey: ['ops-capabilities'],
    queryFn: () => api.get<OpsCapabilities>('/api/v1/dashboard/ops/capabilities'),
  })
  const gitPullCapability = capabilitiesQuery.data?.git_pull
  const gitPullAvailable = gitPullCapability?.available === true

  const runAction = async (action: 'restart' | 'pull') => {
    setConfirmAction(null)
    setTask(null)
    try {
      const body = action === 'restart'
        ? await api.post<OpsResult>('/api/v1/dashboard/ops/restart')
        : await api.post<OpsResult>('/api/v1/dashboard/ops/git-pull')
      setTask(body)
      if (action === 'restart') {
        toast.info('重启指令已下发', '连接可能短暂中断')
      } else {
        toast.success('代码已更新')
        await queryClient.invalidateQueries({ queryKey: ['ops-logs'] })
      }
    } catch (error) {
      const failedTask = error instanceof ApiError && isOpsResult(error.data) && error.data.started
        ? error.data
        : null
      setTask(failedTask)
      toast.error(
        failedTask ? '操作执行失败' : '指令未下发',
        error instanceof ApiError ? error.message : '请检查运行环境与服务状态',
      )
    }
  }

  const runDiagnose = async () => {
    setDiagnosing(true)
    try {
      setDiagnose(await api.post<DiagnoseResult>('/api/v1/dashboard/ops/diagnose'))
    } catch {
      toast.error('自检失败', '接口不可用')
    } finally {
      setDiagnosing(false)
    }
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold text-text">一键运维</h1>

      <div className="grid gap-4 md:grid-cols-3">
        <Card hover className="p-5">
          <div className="flex items-center gap-2.5">
            <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-accent/15 text-accent"><RefreshCcw size={16} /></span>
            <h3 className="text-sm font-semibold text-text">重启服务</h3>
          </div>
          <p className="mt-2 text-xs leading-relaxed text-muted">重启 achord-review 容器。服务将短暂中断，正在执行的审查可能被终止，请在低峰期操作。</p>
          <button
            onClick={() => setConfirmAction('restart')}
            className="mt-4 w-full rounded-lg border border-line py-2 text-xs font-medium text-text transition-colors hover:bg-surface-2"
          >
            重启容器
          </button>
        </Card>
        <Card hover className="p-5">
          <div className="flex items-center gap-2.5">
            <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-good/15 text-good"><GitPullRequestArrow size={16} /></span>
            <h3 className="text-sm font-semibold text-text">更新代码</h3>
          </div>
          <p className="mt-2 text-xs leading-relaxed text-muted">
            {gitPullCapability?.reason ?? '正在检测受控 Git 工作区…'}
          </p>
          <button
            onClick={() => setConfirmAction('pull')}
            disabled={!gitPullAvailable}
            className="mt-4 w-full rounded-lg border border-line py-2 text-xs font-medium text-text transition-colors hover:bg-surface-2 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {capabilitiesQuery.isLoading ? '检测中…' : gitPullAvailable ? '拉取更新' : '由宿主机更新'}
          </button>
        </Card>
        <Card hover className="p-5">
          <div className="flex items-center gap-2.5">
            <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-info/15 text-info"><HeartPulse size={16} /></span>
            <h3 className="text-sm font-semibold text-text">系统自检</h3>
          </div>
          <p className="mt-2 text-xs leading-relaxed text-muted">探测 LLM 中继连通性、GitHub App 凭据与本地存储健康度，逐项给出结果。</p>
          <button
            onClick={() => void runDiagnose()}
            disabled={diagnosing}
            className="mt-4 w-full rounded-lg border border-line py-2 text-xs font-medium text-text transition-colors hover:bg-surface-2 disabled:opacity-40"
          >
            {diagnosing ? '检测中…' : '开始自检'}
          </button>
        </Card>
      </div>

      {(task || diagnose) && (
        <div className="grid gap-4 lg:grid-cols-2">
          {task && (
            <Card>
              <CardHeader
                title="任务输出"
                description={task.completed ? `已结束（exit code ${task.exit_code ?? '?'}）` : '指令已下发'}
              />
              <pre className={`max-h-60 overflow-auto whitespace-pre-wrap break-all px-5 py-4 font-mono text-xs leading-relaxed ${task.exit_code !== null && task.exit_code !== 0 ? 'text-bad' : 'text-muted'}`}>
                {task.output.join('\n') || '（暂无输出）'}
              </pre>
            </Card>
          )}
          {diagnose && (
            <Card>
              <CardHeader title="自检结果" description={diagnose.ok ? '全部通过' : '存在异常项'} />
              <div>
                <ProbeRow name="LLM 中继" result={diagnose.llm} />
                <ProbeRow name="GitHub App 凭据" result={diagnose.github_app} />
                <ProbeRow name="本地存储" result={diagnose.storage} />
              </div>
            </Card>
          )}
        </div>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader
            title="实时日志"
            description="最近 200 行，每 5 秒刷新"
            action={<Terminal size={14} className="text-muted" />}
          />
          <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-all px-5 py-4 font-mono text-[11px] leading-relaxed text-muted">
            {logsQuery.isLoading
              ? '加载中…'
              : (logsQuery.data?.lines ?? []).join('\n') || '（暂无日志输出 — 需在部署环境设置 ACHORD_REVIEW_LOG_FILE）'}
          </pre>
        </Card>

        <Card>
          <CardHeader
            title="操作审计"
            description="面板上的每一次敏感操作都留痕"
            action={<Activity size={14} className="text-muted" />}
          />
          <div className="max-h-96 overflow-y-auto">
            {auditQuery.isLoading ? (
              <div className="space-y-2 p-5">
                {Array.from({ length: 5 }).map((_, i) => <Skeleton key={i} className="h-8" />)}
              </div>
            ) : (auditQuery.data?.items ?? []).length === 0 ? (
              <p className="py-10 text-center text-xs text-muted">暂无操作记录</p>
            ) : (
              <ul className="divide-y divide-line/60">
                {(auditQuery.data?.items ?? []).map((log) => (
                  <li key={log.id} className="flex items-center justify-between px-5 py-2.5 text-xs">
                    <div className="min-w-0">
                      <span className="font-medium text-text">{log.action}</span>
                      {log.details_json && log.details_json !== '{}' && (
                        <span className="ml-2 truncate text-muted">{log.details_json}</span>
                      )}
                    </div>
                    <div className="shrink-0 text-right text-muted">
                      <span>{formatDateTime(log.created_at)}</span>
                      {log.ip_address && <span className="ml-2 font-mono">{log.ip_address}</span>}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </Card>
      </div>

      <ConfirmDialog
        open={confirmAction === 'restart'}
        title="重启 achord-review 容器？"
        body="服务将短暂中断，正在执行的审查可能被终止。确认当前没有重要审查后再继续。"
        danger
        confirmLabel="确认重启"
        onConfirm={() => void runAction('restart')}
        onCancel={() => setConfirmAction(null)}
      />
      <ConfirmDialog
        open={confirmAction === 'pull'}
        title="拉取最新代码？"
        body="将执行 git pull --ff-only。仅 fast-forward 更新，不会自动合并冲突。"
        confirmLabel="确认拉取"
        onConfirm={() => void runAction('pull')}
        onCancel={() => setConfirmAction(null)}
      />
    </div>
  )
}
