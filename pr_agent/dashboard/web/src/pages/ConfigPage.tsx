import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { KeyRound, RotateCcw, Save } from 'lucide-react'
import { api, ApiError } from '../lib/api'
import type { ConfigData, ConfigValues } from '../lib/types'
import { Card, CardHeader, Skeleton } from '../components/ui'
import { ConfirmDialog } from '../components/Dialogs'
import { useToast } from '../components/Toast'
import { useAuth } from '../lib/auth'
import { waitForServiceThenReload } from '../lib/restart'

const SEVERITIES = ['P0', 'P1', 'P2', 'P3'] as const

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="block text-xs font-medium uppercase tracking-wider text-muted">{label}</label>
      {children}
      {hint && <p className="mt-1 text-[11px] leading-relaxed text-muted">{hint}</p>}
    </div>
  )
}

const inputClass =
  'mt-1.5 w-full rounded-lg border border-line bg-surface-2 px-3.5 py-2.5 text-sm text-text placeholder-muted/50 outline-none transition-colors focus:border-accent focus:ring-2 focus:ring-accent/20'

export default function ConfigPage() {
  const toast = useToast()
  const { refresh: refreshAuth } = useAuth()
  const queryClient = useQueryClient()
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['config'],
    queryFn: () => api.get<ConfigData>('/api/v1/dashboard/config'),
  })

  const [values, setValues] = useState<Partial<ConfigValues>>({})
  // The API returns only a masked key ("sk-se****7890"). It is shown as
  // placeholder text and never put back into the payload: an empty keySecret
  // means "keep the stored secret", a typed value replaces it.
  const [keySecret, setKeySecret] = useState('')
  const [dirtyFields, setDirtyFields] = useState<Set<keyof ConfigValues>>(new Set())
  const [saving, setSaving] = useState(false)
  const saveInFlight = useRef(false)
  const [confirmRestart, setConfirmRestart] = useState(false)
  const [newGlob, setNewGlob] = useState('')
  const [upstreamModels, setUpstreamModels] = useState<string[]>([])
  const [fetchingModels, setFetchingModels] = useState(false)

  const fetchUpstreamModels = async () => {
    if (fetchingModels) return
    setFetchingModels(true)
    try {
      const body = await api.get<{ models: string[] }>('/api/v1/dashboard/config/upstream-models')
      setUpstreamModels(body.models ?? [])
      toast.success('已获取上游模型', `共 ${body.models?.length ?? 0} 个，可在模型输入框下拉选择`)
    } catch (err) {
      toast.error('获取上游模型失败', err instanceof ApiError ? err.message : '请检查中继 API Base 与密钥')
    } finally {
      setFetchingModels(false)
    }
  }

  useEffect(() => {
    if (data?.values) {
      setValues(data.values)
      setKeySecret('')
      setDirtyFields(new Set())
    }
  }, [data])

  const set = <K extends keyof ConfigValues>(key: K, value: ConfigValues[K]) => {
    setValues((prev) => ({ ...prev, [key]: value }))
    setDirtyFields((prev) => new Set(prev).add(key))
  }

  const save = async (restart: boolean) => {
    if (saveInFlight.current) return
    saveInFlight.current = true
    setSaving(true)
    try {
      const payload: Record<string, unknown> = { restart }
      dirtyFields.forEach((key) => {
        if (key !== 'key') payload[key] = values[key]
      })
      // a typed replacement is submitted; empty means "keep the stored secret"
      if (keySecret) payload.key = keySecret
      const body = await api.put<{
        restarted: boolean
        restart_started: boolean
        restart_output: string[]
        hot_reload_pending: boolean
        auth_sync_warning: string
        reload_warning: string
        persistence_warning: string
      }>('/api/v1/dashboard/config', payload)
      if (body.auth_sync_warning) {
        toast.error('配置已保存，但认证状态同步失败', body.auth_sync_warning)
      } else if (body.hot_reload_pending && !body.restart_started) {
        toast.error('配置已保存，但尚未热生效', body.reload_warning || '请重启服务后生效')
      } else if (body.persistence_warning) {
        const restartNote = body.restart_started ? '；重启指令已下发，完成状态待确认' : ''
        toast.info('配置已保存，但持久化确认失败', `${body.persistence_warning}${restartNote}`)
      } else if (restart && !body.restart_started) {
        toast.error('配置已保存，但重启未发起', body.restart_output[0] ?? '请在宿主机重启服务')
      } else if (body.restart_started && !body.restarted) {
        toast.success('配置已保存，重启指令已下发', '完成状态尚未确认，请稍后手动刷新页面')
      } else {
        toast.success('配置已保存', body.restarted ? '容器已完成重启，页面将在 30 秒后自动刷新' : '变更已热生效，无需重启')
      }
      setDirtyFields(new Set())
      setKeySecret('')
      setConfirmRestart(false)
      if (body.restarted) {
        window.setTimeout(() => window.location.reload(), 30_000)
      } else if (body.restart_started) {
        queryClient.cancelQueries()
        void waitForServiceThenReload()
      } else {
        await queryClient.invalidateQueries({ queryKey: ['config'] })
        await refreshAuth()
      }
    } catch (err) {
      toast.error('保存失败', err instanceof ApiError ? err.message : '未知错误')
    } finally {
      saveInFlight.current = false
      setSaving(false)
    }
  }

  if (isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-40" />
        <Skeleton className="h-96" />
      </div>
    )
  }
  if (isError || !data) {
    return (
      <Card className="p-10 text-center">
        <p className="text-sm text-muted">配置加载失败</p>
        <button onClick={() => void refetch()} className="mt-3 rounded-lg border border-line px-4 py-2 text-sm text-text hover:bg-surface-2">重试</button>
      </Card>
    )
  }
  if (!data.available) {
    return (
      <Card className="p-10 text-center">
        <p className="text-sm text-text">未找到配置文件</p>
        <p className="mt-1 text-xs text-muted">路径：{data.path ?? '未探测到'}（容器内应为 /app/pr_agent/settings_prod/.secrets.toml）</p>
      </Card>
    )
  }

  const sevSelected = values.verdict_blocking_severities ?? []
  const dirty = dirtyFields.size > 0

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-text">配置中心</h1>
          <p className="mt-0.5 text-xs text-muted">
            编辑 <span className="font-mono">{data.path}</span> · 保存时自动备份并热生效
          </p>
        </div>
        <div className="flex items-center gap-2">
          {dirty && <span className="text-xs text-warn">有未保存的修改</span>}
          <button
            onClick={() => setConfirmRestart(true)}
            disabled={!dirty || saving}
            className="flex items-center gap-1.5 rounded-lg border border-warn/40 px-3.5 py-2 text-xs font-medium text-warn transition-colors hover:bg-warn/10 disabled:opacity-40"
          >
            <RotateCcw size={13} /> 保存并重启
          </button>
          <button
            onClick={() => void save(false)}
            disabled={!dirty || saving}
            className="flex items-center gap-1.5 rounded-lg bg-accent-strong px-4 py-2 text-xs font-semibold text-white transition-all hover:bg-accent active:scale-[0.98] disabled:opacity-40"
          >
            {saving ? <span className="h-3 w-3 animate-spin rounded-full border-2 border-white/40 border-t-white" /> : <Save size={13} />}
            {saving ? '保存中…' : '保存配置'}
          </button>
        </div>
      </div>

      <fieldset disabled={saving} className="contents">
      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader title="模型" description="审查引擎使用的 LLM 与推理参数" />
          <div className="space-y-4 p-5">
            <Field label="模型" hint="直接填模型名（如 gpt-5.6-sol），无需 openai/ 前缀。若中继非官方 OpenAI，请在「中继与密钥」把 Provider 适配设为 openai。">
              <div className="flex items-center gap-2">
                <input
                  className={inputClass}
                  list="upstream-models"
                  value={values.model ?? ''}
                  onChange={(e) => set('model', e.target.value)}
                />
                <button
                  type="button"
                  onClick={() => void fetchUpstreamModels()}
                  disabled={fetchingModels}
                  className="mt-1.5 shrink-0 whitespace-nowrap rounded-lg border border-line px-3 py-2.5 text-xs font-medium text-muted transition-colors hover:bg-surface-2 hover:text-text disabled:opacity-50"
                >
                  {fetchingModels ? '获取中…' : '获取上游模型'}
                </button>
              </div>
              {upstreamModels.length > 0 && (
                <datalist id="upstream-models">
                  {upstreamModels.map((m) => <option key={m} value={m} />)}
                </datalist>
              )}
            </Field>
            <Field label="推理强度" hint="支持 none / minimal / low / medium / high / xhigh / max">
              <select className={inputClass} value={values.reasoning_effort ?? ''} onChange={(e) => set('reasoning_effort', e.target.value)}>
                <option value="">默认</option>
                {['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'].map((v) => <option key={v} value={v}>{v}</option>)}
              </select>
            </Field>
            <div className="grid grid-cols-2 gap-4">
              <Field label="AI 超时（秒）">
                <input type="number" className={inputClass} value={values.ai_timeout ?? ''} onChange={(e) => set('ai_timeout', e.target.value === '' ? null : Number(e.target.value))} />
              </Field>
              <Field label="最大模型 Token">
                <input type="number" className={inputClass} value={values.max_model_tokens ?? ''} onChange={(e) => set('max_model_tokens', e.target.value === '' ? null : Number(e.target.value))} />
              </Field>
            </div>
          </div>
        </Card>

        <Card>
          <CardHeader title="中继与密钥" description="OpenAI 兼容中继端点；密钥留空表示保持不变" />
          <div className="space-y-4 p-5">
            <Field label="API Base">
              <input className={inputClass} value={values.api_base ?? ''} onChange={(e) => set('api_base', e.target.value)} />
            </Field>
            <Field label="API Key" hint={values.key ? `当前：${values.key}` : '未设置'}>
              <div className="relative">
                <KeyRound size={13} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted" />
                <input
                  type="password"
                  className={`${inputClass} pl-9`}
                  placeholder="留空保持现有密钥不变"
                  value={keySecret}
                  onChange={(e) => {
                    const value = e.target.value
                    setKeySecret(value)
                    setDirtyFields((prev) => {
                      const next = new Set(prev)
                      if (value) next.add('key')
                      else next.delete('key')
                      return next
                    })
                  }}
                />
              </div>
            </Field>
            <Field label="Provider 适配" hint="OpenAI 兼容中继填 openai，即可用裸模型名（免 openai/ 前缀）；留空则由模型名自动推断路由。">
              <input
                className={inputClass}
                placeholder="openai"
                value={values.custom_llm_provider ?? ''}
                onChange={(e) => set('custom_llm_provider', e.target.value)}
              />
            </Field>
          </div>
        </Card>

        <Card>
          <CardHeader title="审查门禁" description="决定什么级别的发现会阻断合并" />
          <div className="space-y-4 p-5">
            <Field label="阻断严重度" hint="勾选的严重度将触发 REQUEST_CHANGES 裁决">
              <div className="mt-2 flex gap-2">
                {SEVERITIES.map((sev) => {
                  const active = sevSelected.includes(sev)
                  return (
                    <button
                      key={sev}
                      type="button"
                      onClick={() =>
                        set('verdict_blocking_severities',
                          active ? sevSelected.filter((s) => s !== sev) : [...sevSelected, sev].sort())
                      }
                      className={`rounded-lg border px-4 py-2 text-sm font-semibold transition-all ${
                        active
                          ? sev === 'P0' ? 'border-bad bg-bad/15 text-bad'
                            : sev === 'P1' ? 'border-warn bg-warn/15 text-warn'
                              : sev === 'P2' ? 'border-info bg-info/15 text-info'
                                : 'border-muted bg-muted/15 text-muted'
                          : 'border-line bg-surface-2 text-muted hover:text-text'
                      }`}
                    >
                      {sev}
                    </button>
                  )
                })}
              </div>
            </Field>
            <Field label="最大发现数" hint="单次审查报告的问题上限（1-30），是天花板不是配额">
              <input type="number" className={inputClass} value={values.num_max_findings ?? ''} onChange={(e) => set('num_max_findings', e.target.value === '' ? null : Number(e.target.value))} />
            </Field>
          </div>
        </Card>

        <Card>
          <CardHeader title="忽略规则" description="ignore.glob：命中的文件不参与审查" />
          <div className="space-y-3 p-5">
            <div className="flex gap-2">
              <input
                className={inputClass}
                placeholder="如 dist/**, *.min.js"
                value={newGlob}
                onChange={(e) => setNewGlob(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && newGlob.trim()) {
                    set('ignore_glob', [...(values.ignore_glob ?? []), newGlob.trim()])
                    setNewGlob('')
                  }
                }}
              />
              <button
                type="button"
                onClick={() => {
                  if (newGlob.trim()) {
                    set('ignore_glob', [...(values.ignore_glob ?? []), newGlob.trim()])
                    setNewGlob('')
                  }
                }}
                className="mt-1.5 shrink-0 rounded-lg border border-line px-4 text-sm text-muted transition-colors hover:bg-surface-3 hover:text-text"
              >
                添加
              </button>
            </div>
            {(values.ignore_glob ?? []).length === 0 ? (
              <p className="py-3 text-center text-xs text-muted">暂无忽略规则</p>
            ) : (
              <ul className="space-y-1.5">
                {(values.ignore_glob ?? []).map((glob, index) => (
                  <li key={`${glob}-${index}`} className="flex items-center justify-between rounded-lg border border-line bg-surface-2 px-3 py-2">
                    <code className="text-xs text-text">{glob}</code>
                    <button
                      onClick={() => set('ignore_glob', (values.ignore_glob ?? []).filter((_, i) => i !== index))}
                      className="text-xs text-muted transition-colors hover:text-bad"
                    >
                      移除
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </Card>
      </div>

      <Card>
        <CardHeader title="Extra Instructions" description="注入审查提示词的附加指令（噪音规则、严重度政策等）" />
        <div className="p-5">
          <textarea
            className={`${inputClass} min-h-44 font-mono text-xs leading-relaxed`}
            value={values.extra_instructions ?? ''}
            onChange={(e) => set('extra_instructions', e.target.value)}
          />
        </div>
      </Card>
      </fieldset>

      <ConfirmDialog
        open={confirmRestart}
        title="保存并重启容器？"
        body="部分配置项仅在进程启动时读取。保存后容器将自动重启，服务中断约 10-30 秒，期间 webhook 不响应。"
        danger
        confirmLabel="保存并重启"
        onConfirm={() => void save(true)}
        onCancel={() => setConfirmRestart(false)}
      />
    </div>
  )
}
