import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  CheckCircle2, CircleArrowUp, LoaderCircle, RefreshCw, Rocket, Sparkles, X,
} from 'lucide-react'
import { api, ApiError } from '../lib/api'
import type { OpsCapabilities, OpsResult, VersionInfo } from '../lib/types'
import { waitForServiceThenReload } from '../lib/restart'
import { useToast } from './Toast'

function StatusLine({ tone, children }: {
  tone: 'good' | 'accent' | 'warn'
  children: React.ReactNode
}) {
  const cls =
    tone === 'accent'
      ? 'border-accent/30 bg-accent/10 text-accent'
      : tone === 'warn'
        ? 'border-warn/30 bg-warn/10 text-warn'
        : 'border-good/30 bg-good/10 text-good'
  return (
    <div className={`flex items-start gap-2 rounded-lg border px-4 py-3 text-xs font-medium leading-relaxed ${cls}`}>
      {children}
    </div>
  )
}

/**
 * Version & update panel — a card anchored under the sidebar version label.
 * Commercial framing: it leads with the product version and only surfaces update
 * details when an update actually exists. No commit hashes or branch names.
 */
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
  const featureEnabled = info?.available === true
  const restartAvailable = capabilitiesQuery.data?.restart.available === true
  const updateAvailable = info?.update_available === true
  const staged = info?.staged === true
  const rebuildRequired = capabilitiesQuery.data?.rebuild_required === true
    || info?.pending?.rebuild_required === true
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
      toast.success('新版本已准备', '重启后生效')
      await Promise.all([updateQuery.refetch(), capabilitiesQuery.refetch()])
      setPhase('idle')
    } catch (error) {
      setPhase('idle')
      toast.error('更新失败', error instanceof ApiError ? error.message : '请稍后重试')
    }
  }

  const runRestart = async () => {
    try {
      await api.post<OpsResult>('/api/v1/dashboard/ops/restart')
      setPhase('restarting')
      toast.info('正在重启', '服务恢复后会自动刷新，无需重新登录')
      queryClient.cancelQueries()
      void waitForServiceThenReload()
    } catch (error) {
      toast.error('重启未发起', error instanceof ApiError ? error.message : '请稍后重试')
    }
  }

  return (
    <>
      {/* Click-catcher: dismiss on outside click, no dimming. Always mounted so the
          trigger underneath cannot be clicked; during a restart it blocks without
          dismissing, keeping the service-recovery polling effect alive. */}
      <div
        className="fixed inset-0 z-40"
        onClick={phase === 'restarting' ? undefined : onClose}
        aria-hidden="true"
      />
      <div
        role="dialog"
        aria-label="版本与更新"
        className="animate-fade-in fixed left-3 top-[58px] z-50 w-[320px] max-w-[calc(100vw-1.5rem)] rounded-xl border border-line bg-surface-1 p-5 shadow-2xl"
      >
        {phase === 'restarting' ? (
          <div className="flex flex-col items-center py-6 text-center">
            <LoaderCircle size={30} className="animate-spin text-accent" />
            <h3 className="mt-4 text-sm font-semibold text-text">正在重启并刷新…</h3>
            <p className="mt-2 text-xs text-muted">
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
                  <h3 className="text-sm font-semibold text-text">版本与更新</h3>
                  <p className="text-xs text-muted">控制面板 v{version}</p>
                </div>
              </div>
              <div className="flex items-center gap-1">
                <button
                  onClick={() => void updateQuery.refetch()}
                  disabled={updateQuery.isFetching || phase === 'updating'}
                  className="rounded-md p-1 text-muted transition-colors hover:text-text disabled:opacity-50"
                  aria-label="重新检查更新"
                  title="重新检查"
                >
                  <RefreshCw size={15} className={updateQuery.isFetching ? 'animate-spin' : ''} />
                </button>
                <button onClick={onClose} className="rounded-md p-1 text-muted hover:text-text" aria-label="关闭">
                  <X size={16} />
                </button>
              </div>
            </div>

            {updateQuery.isLoading ? (
              <div className="mt-4 flex items-center gap-2 rounded-lg border border-line bg-surface-2/50 px-4 py-4 text-sm text-muted">
                <LoaderCircle size={15} className="animate-spin" /> 正在检查更新…
              </div>
            ) : updateQuery.isError ? (
              <div className="mt-4 rounded-lg border border-line bg-surface-2/50 px-4 py-3">
                <p className="text-xs leading-relaxed text-warn">
                  无法检查更新{updateQuery.error instanceof ApiError ? `：${updateQuery.error.message}` : ''}
                </p>
                <button
                  onClick={() => void updateQuery.refetch()}
                  disabled={updateQuery.isFetching}
                  className="mt-2 flex items-center gap-1.5 rounded-lg border border-line px-3 py-1.5 text-xs font-medium text-muted transition-colors hover:bg-surface-2 hover:text-text disabled:opacity-50"
                >
                  <RefreshCw size={13} className={updateQuery.isFetching ? 'animate-spin' : ''} />
                  重试
                </button>
              </div>
            ) : !featureEnabled ? (
              <p className="mt-4 rounded-lg border border-line bg-surface-2/50 px-4 py-3 text-xs leading-relaxed text-muted">
                {info?.reason ?? '更新由宿主机发布流程管理，面板内更新未启用。'}
              </p>
            ) : (
              <>
                <div className="mt-4">
                  {!info?.checked ? (
                    <p className="rounded-lg border border-line bg-surface-2/50 px-4 py-3 text-xs text-muted">
                      {info?.reason ?? '暂时无法检查更新。'}
                    </p>
                  ) : updateAvailable ? (
                    <StatusLine tone="accent"><Sparkles size={14} /> 发现新版本，可立即更新</StatusLine>
                  ) : staged && rebuildRequired ? (
                    <StatusLine tone="warn">⚠ 新版本已准备，但本次更新需由维护者在服务器完成</StatusLine>
                  ) : staged ? (
                    <StatusLine tone="good"><CheckCircle2 size={14} /> 新版本已准备，重启后生效</StatusLine>
                  ) : info.diverged ? (
                    <StatusLine tone="warn">⚠ 版本与远端不一致，请联系维护者处理</StatusLine>
                  ) : rebuildRequired ? (
                    <StatusLine tone="warn">⚠ 运行环境与最新版本不一致，请联系维护者处理</StatusLine>
                  ) : aheadOnly ? (
                    <StatusLine tone="warn">⚠ 存在尚未发布的改动</StatusLine>
                  ) : (
                    <StatusLine tone="good"><CheckCircle2 size={14} /> 已是最新版本</StatusLine>
                  )}
                </div>

                {/* Actions only when there is one to take; re-checking now lives as
                    the header icon, so the row is absent when the panel is idle. */}
                {(updateAvailable || (staged && !rebuildRequired)) && (
                  <div className="mt-5 flex items-center justify-end gap-2.5">
                    {updateAvailable && (
                      <button
                        onClick={() => void runUpdate()}
                        disabled={phase === 'updating' || updateQuery.isFetching}
                        className="flex items-center gap-1.5 rounded-lg bg-accent-strong px-4 py-2 text-sm font-semibold text-white transition-all hover:bg-accent active:scale-[0.98] disabled:opacity-50"
                      >
                        {phase === 'updating'
                          ? <><LoaderCircle size={14} className="animate-spin" /> 更新中…</>
                          : <><CircleArrowUp size={14} /> 一键更新</>}
                      </button>
                    )}
                    {/* Restart is only offered to apply a prepared update — there is no
                        routine need to restart, so it does not sit here permanently. */}
                    {staged && !rebuildRequired && (
                      <button
                        onClick={() => void runRestart()}
                        disabled={!restartAvailable || phase === 'updating' || updateQuery.isFetching}
                        title={restartAvailable ? '' : capabilitiesQuery.data?.restart.reason}
                        className="flex items-center gap-1.5 rounded-lg bg-accent-strong px-4 py-2 text-sm font-semibold text-white transition-all hover:bg-accent active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40"
                      >
                        <RefreshCw size={14} />
                        重启以生效
                      </button>
                    )}
                  </div>
                )}
              </>
            )}
          </>
        )}
      </div>
    </>
  )
}
