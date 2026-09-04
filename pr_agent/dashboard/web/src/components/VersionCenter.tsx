import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  CheckCircle2, ChevronDown, CircleArrowUp, LoaderCircle, RefreshCw, Rocket, Sparkles, X,
} from 'lucide-react'
import { api, ApiError } from '../lib/api'
import type { OpsCapabilities, OpsResult, VersionInfo } from '../lib/types'
import { waitForServiceThenReload } from '../lib/restart'
import { useToast } from './Toast'

function CommitLine({ label, sha, subject, tone }: {
  label: string
  sha: string | null | undefined
  subject: string | null | undefined
  tone?: 'accent'
}) {
  return (
    <div className="flex items-start justify-between gap-3 py-2">
      <span className="shrink-0 text-xs text-muted">{label}</span>
      <div className="min-w-0 text-right">
        <span className={`font-mono text-xs ${tone === 'accent' ? 'text-accent' : 'text-text'}`}>
          {sha ?? '—'}
        </span>
        {subject && <p className="truncate text-[11px] text-muted" title={subject}>{subject}</p>}
      </div>
    </div>
  )
}

export function VersionCenter({ onClose, version }: {
  onClose: () => void
  version: string
}) {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [phase, setPhase] = useState<'idle' | 'updating' | 'restarting'>('idle')

  const updateQuery = useQuery({
    queryKey: ['ops-check-update'],
    queryFn: () => api.get<VersionInfo>('/api/v1/dashboard/ops/check-update'),
    refetchOnWindowFocus: false,
    staleTime: 30_000,
  })
  const capabilitiesQuery = useQuery({
    queryKey: ['ops-capabilities'],
    queryFn: () => api.get<OpsCapabilities>('/api/v1/dashboard/ops/capabilities'),
  })

  const info = updateQuery.data
  const restartAvailable = capabilitiesQuery.data?.restart.available === true
  const updateAvailable = info?.update_available === true
  // The server reports a prepared release separately from "update available", so
  // the panel never re-offers a revision that is already staged for restart.
  const staged = info?.staged === true
  const pending = info?.pending ?? null
  // Rebuild-required is server-authoritative (computed against the staged release),
  // so it survives a reopen; it blocks an in-place restart.
  const rebuildRequired = capabilitiesQuery.data?.rebuild_required === true
    || pending?.rebuild_required === true
  const aheadOnly = info?.checked === true && !updateAvailable && !info.diverged
    && (info.ahead ?? 0) > 0

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && phase !== 'restarting') onClose()
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onClose, phase])

  const runUpdate = async () => {
    setPhase('updating')
    try {
      const result = await api.post<OpsResult>('/api/v1/dashboard/ops/git-pull')
      // The backend maps a not-started / failed OpsResult to 503 / 500, but never
      // trust a 200 alone: only a completed, zero-exit result means staging happened.
      if (!result.started || !result.completed || result.exit_code !== 0) {
        throw new ApiError(200, (result.output ?? []).slice(-1)[0] || '更新未完成')
      }
      toast.success('新版本已准备', (result.output ?? []).slice(-1)[0] || '重启后生效')
      await Promise.all([updateQuery.refetch(), capabilitiesQuery.refetch()])
      setPhase('idle')
    } catch (error) {
      setPhase('idle')
      toast.error('更新失败', error instanceof ApiError ? error.message : '请检查受控 Git 工作区')
    }
  }

  const runRestart = async () => {
    try {
      await api.post<OpsResult>('/api/v1/dashboard/ops/restart')
      setPhase('restarting')
      toast.info('正在重启', '服务恢复后会自动刷新，无需重新登录')
      // stop background polling so it doesn't spam errors during downtime
      queryClient.cancelQueries()
      void waitForServiceThenReload()
    } catch (error) {
      toast.error('重启未发起', error instanceof ApiError ? error.message : '请检查受控 Docker 端点')
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div
        className="absolute inset-0 bg-black/60 backdrop-blur-sm"
        onClick={() => phase !== 'restarting' && onClose()}
      />
      <div className="animate-fade-in relative w-full max-w-md rounded-xl border border-line bg-surface-1 p-6 shadow-2xl">
        {phase === 'restarting' ? (
          <div className="flex flex-col items-center py-8 text-center">
            <LoaderCircle size={32} className="animate-spin text-accent" />
            <h3 className="mt-4 text-base font-semibold text-text">正在重启并刷新…</h3>
            <p className="mt-2 text-sm text-muted">
              服务恢复后本页会自动刷新，登录状态保留，无需重新登录。
            </p>
          </div>
        ) : (
          <>
            <div className="flex items-start justify-between gap-3">
              <div className="flex items-center gap-2.5">
                <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-accent/15 text-accent">
                  {updateAvailable ? <Sparkles size={18} /> : <Rocket size={18} />}
                </span>
                <div>
                  <h3 className="text-base font-semibold text-text">版本与更新</h3>
                  <p className="text-xs text-muted">控制面板 v{version}</p>
                </div>
              </div>
              <button onClick={onClose} className="rounded-md p-1 text-muted hover:text-text" aria-label="关闭">
                <X size={16} />
              </button>
            </div>

            <div className="mt-4 rounded-lg border border-line bg-surface-2/50 px-4 py-1.5">
              {updateQuery.isLoading ? (
                <div className="flex items-center gap-2 py-4 text-sm text-muted">
                  <LoaderCircle size={15} className="animate-spin" /> 正在检查更新…
                </div>
              ) : info?.checked ? (
                <>
                  <CommitLine label="当前版本" sha={info.current?.sha} subject={info.current?.subject} />
                  <div className="border-t border-line/60" />
                  <CommitLine
                    label="最新版本"
                    sha={info.latest?.sha}
                    subject={info.latest?.subject}
                    tone={updateAvailable ? 'accent' : undefined}
                  />
                  {pending && (
                    <>
                      <div className="border-t border-line/60" />
                      <CommitLine label="已准备" sha={pending.sha} subject={pending.subject} tone="accent" />
                    </>
                  )}
                </>
              ) : (
                <p className="py-4 text-sm text-muted">{info?.reason ?? '暂时无法检查更新。'}</p>
              )}
            </div>

            {info?.checked && (
              <div className="mt-3">
                {updateAvailable ? (
                  <p className="flex items-center gap-1.5 text-xs font-medium text-accent">
                    <CircleArrowUp size={14} />
                    发现新版本，落后 {info.behind} 个提交
                  </p>
                ) : staged && rebuildRequired ? (
                  <p className="text-xs font-medium text-warn">
                    ⚠ 新版本 {pending?.sha} 已准备，但改动了依赖/构建文件，重启不生效——
                    请在宿主机执行 <code className="font-mono">git pull --ff-only &amp;&amp; docker compose up -d --build</code>
                  </p>
                ) : staged ? (
                  <p className="flex items-center gap-1.5 text-xs font-medium text-good">
                    <CheckCircle2 size={14} /> 新版本 {pending?.sha} 已准备，重启后生效
                  </p>
                ) : info.diverged ? (
                  <p className="text-xs font-medium text-warn">
                    ⚠ 本地与远端已分叉（本地领先 {info.ahead}、落后 {info.behind}），
                    无法一键 fast-forward 更新，请在宿主机处理
                  </p>
                ) : rebuildRequired ? (
                  <p className="text-xs font-medium text-warn">
                    ⚠ 运行镜像与检出依赖不一致，重启已被禁用——
                    请在宿主机执行 <code className="font-mono">git pull --ff-only &amp;&amp; docker compose up -d --build</code>
                  </p>
                ) : aheadOnly ? (
                  <p className="text-xs font-medium text-warn">
                    ⚠ 本地领先远端 {info.ahead} 个提交（有未推送的本地改动），与远端不一致
                  </p>
                ) : (
                  <p className="flex items-center gap-1.5 text-xs text-good">
                    <CheckCircle2 size={14} /> 已是最新版本
                  </p>
                )}
              </div>
            )}

            <div className="mt-5 flex items-center justify-between gap-2.5">
              <button
                onClick={() => void updateQuery.refetch()}
                disabled={updateQuery.isFetching || phase === 'updating'}
                className="flex items-center gap-1.5 rounded-lg border border-line px-3 py-2 text-xs font-medium text-muted transition-colors hover:bg-surface-2 hover:text-text disabled:opacity-50"
              >
                <RefreshCw size={13} className={updateQuery.isFetching ? 'animate-spin' : ''} />
                重新检查
              </button>
              <div className="flex items-center gap-2.5">
                {updateAvailable && (
                  <button
                    onClick={() => void runUpdate()}
                    disabled={phase === 'updating' || updateQuery.isFetching || !info?.available}
                    className="flex items-center gap-1.5 rounded-lg bg-accent-strong px-4 py-2 text-sm font-semibold text-white transition-all hover:bg-accent active:scale-[0.98] disabled:opacity-50"
                  >
                    {phase === 'updating'
                      ? <><LoaderCircle size={14} className="animate-spin" /> 更新中…</>
                      : <><CircleArrowUp size={14} /> 一键更新</>}
                  </button>
                )}
                <button
                  onClick={() => void runRestart()}
                  disabled={!restartAvailable || phase === 'updating' || updateQuery.isFetching || rebuildRequired}
                  title={rebuildRequired
                    ? '依赖与运行镜像不一致，重启会因缺少新依赖导入失败并进入重启循环，请在宿主机重建镜像'
                    : (restartAvailable ? '' : capabilitiesQuery.data?.restart.reason)}
                  className={`flex items-center gap-1.5 rounded-lg px-4 py-2 text-sm font-semibold transition-all active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40 ${
                    staged && !rebuildRequired
                      ? 'bg-accent-strong text-white hover:bg-accent'
                      : 'border border-line text-text hover:bg-surface-2'
                  }`}
                >
                  <RefreshCw size={14} />
                  {rebuildRequired ? '需宿主机重建' : staged ? '重启以生效' : '重启'}
                </button>
              </div>
            </div>
            {!restartAvailable && capabilitiesQuery.data && (
              <p className="mt-2 text-right text-[11px] text-muted">
                {capabilitiesQuery.data.restart.reason}
              </p>
            )}
          </>
        )}
      </div>
    </div>
  )
}

/** Clickable version chip for the top bar. */
export function VersionBadge({ version, updateAvailable, onClick }: {
  version: string
  updateAvailable: boolean
  onClick: () => void
}) {
  return (
    <button
      onClick={onClick}
      className="relative inline-flex items-center gap-1.5 rounded-md border border-line bg-surface-2 px-2.5 py-1 text-xs text-muted transition-colors hover:border-accent/50 hover:text-text"
      title="版本与更新"
    >
      <span className="font-mono">v{version}</span>
      {updateAvailable
        ? <Sparkles size={12} className="text-accent" />
        : <ChevronDown size={12} />}
      {updateAvailable && (
        <span className="absolute -right-0.5 -top-0.5 h-2 w-2 rounded-full bg-accent ring-2 ring-surface-1" />
      )}
    </button>
  )
}
