import { useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Activity, GitPullRequestArrow, HeartPulse, RefreshCcw, Terminal } from 'lucide-react'
import { api } from '../lib/api'
import type { AuditLogListData, DiagnoseResult, OpsTask } from '../lib/types'
import { Card, CardHeader, Skeleton } from '../components/ui'
import { ConfirmDialog } from '../components/Dialogs'
import { useToast } from '../components/Toast'
import { formatDateTime } from '../lib/format'

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
  const [task, setTask] = useState<OpsTask | null>(null)
  const [diagnose, setDiagnose] = useState<DiagnoseResult | null>(null)
  const [diagnosing, setDiagnosing] = useState(false)
  const pollTimer = useRef<number | null>(null)

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

  const pollTaskResult = async (taskId: string) => {
    if (pollTimer.current) window.clearInterval(pollTimer.current)
    pollTimer.current = window.setInterval(async () => {
      try {
        const result = await api.get<OpsTask>(`/api/v1/dashboard/ops/task/${taskId}`)
        setTask(result)
        if (!result.running) {
          if (pollTimer.current) window.clearInterval(pollTimer.current)
          if (result.exit_code === 0) {
            toast.success(taskId === 'restart' ? '容器已重启' : '代码已更新')
            await queryClient.invalidateQueries({ queryKey: ['ops-logs'] })
          } else if (result.exists) {
            toast.error('命令执行失败', `exit code ${result.exit_code}`)
          }
        }
      } catch {
        // restarting the container kills this process mid-poll; the next login shows the result
        if (pollTimer.current) window.clearInterval(pollTimer.current)
      }
    }, 2_000)
  }

  const runAction = async (action: 'restart' | 'pull') => {
    setConfirmAction(null)
    try {
      const body = action === 'restart'
        ? await api.post<{ task_id: string }>('/api/v1/dashboard/ops/restart')
        : await api.post<{ task_id: string }>('/api/v1/dashboard/ops/git-pull')
      toast.info(action === 'restart' ? '重启指令已下发' : 'git pull 已开始')
      setTask({ running: true, exists: true, exit_code: null, output: [] })
      await pollTaskResult(body.task_id)
    } catch {
      toast.error('指令下发失败', '请检查服务状态')
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
          <p className="mt-2 text-xs leading-relaxed text-muted">重启 achord-review 容器。中断约 10-30 秒，优雅停机保证进行中的审查不被打断。</p>
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
          <p className="mt-2 text-xs leading-relaxed text-muted">git pull --ff-only 拉取最新代码（fast-forward，不自动合并）。更新后通常需要重启生效。</p>
          <button
            onClick={() => setConfirmAction('pull')}
            className="mt-4 w-full rounded-lg border border-line py-2 text-xs font-medium text-text transition-colors hover:bg-surface-2"
          >
            拉取更新
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
              <CardHeader title="任务输出" description={task.running ? '执行中…' : `已结束（exit code ${task.exit_code ?? '?'}）`} />
              <pre className="max-h-60 overflow-auto whitespace-pre-wrap break-all px-5 py-4 font-mono text-xs leading-relaxed text-muted">
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
        body="服务将中断约 10-30 秒。进行中的审查会优雅停机，不会丢失数据。"
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
