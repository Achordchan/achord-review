import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { FlaskConical, Loader2, Play } from 'lucide-react'
import { api, ApiError } from '../lib/api'
import { Card, CardHeader } from '../components/ui'
import { MarkdownView } from '../components/MarkdownView'
import { useToast } from '../components/Toast'

const MODELS = [
  { value: '', label: '使用当前配置' },
  { value: 'openai/gpt-5.6-sol', label: 'gpt-5.6-sol' },
  { value: 'openai/o4-mini', label: 'o4-mini' },
]
const EFFORTS = ['', 'low', 'medium', 'high', 'xhigh']

export default function PlaygroundPage() {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [prUrl, setPrUrl] = useState('')
  const [model, setModel] = useState('')
  const [effort, setEffort] = useState('')
  const [extraInstructions, setExtraInstructions] = useState('')
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const valid = /^https:\/\/github\.com\/[^/]+\/[^/]+\/pull\/\d+/.test(prUrl.trim())

  const run = async () => {
    if (!valid || running) return
    setRunning(true)
    setResult(null)
    setNotice(null)
    try {
      await api.post('/api/v1/dashboard/playground/run', {
        pr_url: prUrl.trim(),
        model: model || undefined,
        reasoning_effort: effort || undefined,
        extra_instructions: extraInstructions.trim() || undefined,
      })
    } catch (err) {
      if (err instanceof ApiError && err.code === 'COMING_SOON') {
        setNotice('演练台执行引擎正在最后联调中（F-01 点亮后开放 SSE 实时日志）。当前可以先在 GitHub PR 里 @achord-review 触发审查，结果会自动出现在「审查历史」中。')
        await queryClient.invalidateQueries({ queryKey: ['reviews'] })
      } else {
        toast.error('触发失败', err instanceof ApiError ? err.message : '未知错误')
      }
    } finally {
      setRunning(false)
    }
  }

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold text-text">PR 手动演练台</h1>
        <p className="mt-0.5 text-xs text-muted">无需在 GitHub 发评论，直接对任意 PR 触发一次审查调试</p>
      </div>

      <Card>
        <CardHeader title="触发参数" description="PR 链接为必填，其余项留空即使用当前全局配置" />
        <div className="space-y-4 p-5">
          <div>
            <label className="block text-xs font-medium uppercase tracking-wider text-muted">PR URL</label>
            <input
              value={prUrl}
              onChange={(e) => setPrUrl(e.target.value)}
              placeholder="https://github.com/owner/repo/pull/13"
              className="mt-1.5 w-full rounded-lg border border-line bg-surface-2 px-3.5 py-2.5 font-mono text-sm text-text placeholder-muted/50 outline-none transition-colors focus:border-accent focus:ring-2 focus:ring-accent/20"
            />
            {prUrl && !valid && (
              <p className="mt-1.5 text-[11px] text-warn">请输入完整的 GitHub PR 链接</p>
            )}
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            <div>
              <label className="block text-xs font-medium uppercase tracking-wider text-muted">临时覆盖模型</label>
              <select
                value={model}
                onChange={(e) => setModel(e.target.value)}
                className="mt-1.5 w-full rounded-lg border border-line bg-surface-2 px-3.5 py-2.5 text-sm text-text outline-none focus:border-accent"
              >
                {MODELS.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium uppercase tracking-wider text-muted">思考强度</label>
              <select
                value={effort}
                onChange={(e) => setEffort(e.target.value)}
                className="mt-1.5 w-full rounded-lg border border-line bg-surface-2 px-3.5 py-2.5 text-sm text-text outline-none focus:border-accent"
              >
                {EFFORTS.map((v) => <option key={v} value={v}>{v || '使用当前配置'}</option>)}
              </select>
            </div>
          </div>
          <div>
            <label className="block text-xs font-medium uppercase tracking-wider text-muted">附加指令（Extra Prompt）</label>
            <textarea
              value={extraInstructions}
              onChange={(e) => setExtraInstructions(e.target.value)}
              placeholder="本次运行临时附加给审查提示词的指令，留空则不附加"
              className="mt-1.5 min-h-24 w-full rounded-lg border border-line bg-surface-2 px-3.5 py-2.5 font-mono text-xs text-text placeholder-muted/50 outline-none focus:border-accent"
            />
          </div>
          <button
            onClick={() => void run()}
            disabled={!valid || running}
            className="flex items-center justify-center gap-2 rounded-lg bg-accent-strong px-5 py-2.5 text-sm font-semibold text-white transition-all hover:bg-accent active:scale-[0.99] disabled:cursor-not-allowed disabled:opacity-40"
          >
            {running ? <Loader2 size={15} className="animate-spin" /> : <Play size={15} />}
            {running ? '审查执行中…' : '开始审查'}
          </button>
        </div>
      </Card>

      {notice && (
        <Card className="border-info/30 bg-info/5 p-5">
          <div className="flex items-start gap-3">
            <FlaskConical size={18} className="mt-0.5 shrink-0 text-info" />
            <div>
              <p className="text-sm font-medium text-text">演练台执行引擎即将开放</p>
              <p className="mt-1 text-xs leading-relaxed text-muted">{notice}</p>
            </div>
          </div>
        </Card>
      )}

      {result && (
        <Card>
          <CardHeader title="审查结果" description="本次运行的完整 Markdown 报告" />
          <div className="px-5 py-4">
            <MarkdownView content={result} />
          </div>
        </Card>
      )}
    </div>
  )
}
