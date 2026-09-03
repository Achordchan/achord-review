import { useState } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { Loader2, ShieldCheck } from 'lucide-react'
import { useAuth } from '../lib/auth'
import { ApiError } from '../lib/api'

export default function LoginPage() {
  const { authenticated, loading, login } = useAuth()
  const location = useLocation()
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const from = (location.state as { from?: { pathname?: string } } | null)?.from?.pathname

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="skeleton h-10 w-10 rounded-full" />
      </div>
    )
  }
  if (authenticated) {
    return <Navigate to={from && from.startsWith('/dashboard') ? from : '/dashboard'} replace />
  }

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!password || submitting) return
    setError('')
    setSubmitting(true)
    try {
      await login(password)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '登录失败，请稍后重试')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="flex h-full items-center justify-center p-4">
      <div className="animate-fade-in w-full max-w-sm">
        <div className="mb-8 flex flex-col items-center">
          <span className="flex h-12 w-12 items-center justify-center rounded-xl bg-gradient-to-br from-accent to-accent-strong shadow-lg shadow-accent/20">
            <ShieldCheck size={24} className="text-white" />
          </span>
          <h1 className="mt-4 text-xl font-semibold text-text">achord-review 控制面板</h1>
          <p className="mt-1 text-sm text-muted">输入管理员口令以继续</p>
        </div>
        <form onSubmit={submit} className="rounded-xl border border-line bg-surface-1 p-6 shadow-xl">
          <label htmlFor="password" className="block text-xs font-medium uppercase tracking-wider text-muted">
            管理员口令
          </label>
          <input
            id="password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="••••••••"
            autoFocus
            autoComplete="current-password"
            className="mt-2 w-full rounded-lg border border-line bg-surface-2 px-3.5 py-2.5 text-sm text-text placeholder-muted/50 outline-none transition-colors focus:border-accent focus:ring-2 focus:ring-accent/25"
          />
          {error && (
            <p className="mt-3 rounded-lg border border-bad/30 bg-bad/10 px-3 py-2 text-xs text-bad" role="alert">
              {error}
            </p>
          )}
          <button
            type="submit"
            disabled={!password || submitting}
            className="mt-5 flex w-full items-center justify-center gap-2 rounded-lg bg-accent-strong py-2.5 text-sm font-semibold text-white transition-all hover:bg-accent active:scale-[0.99] disabled:cursor-not-allowed disabled:opacity-50"
          >
            {submitting && <Loader2 size={15} className="animate-spin" />}
            {submitting ? '验证中…' : '登 录'}
          </button>
        </form>
        <p className="mt-4 text-center text-xs text-muted">
          口令在 config.toml 的 [dashboard] admin_password 中设置
        </p>
      </div>
    </div>
  )
}
