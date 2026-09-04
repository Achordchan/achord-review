import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { api, ApiError, AUTH_INVALIDATED_EVENT, storeToken } from './api'
import type { SessionInfo } from './types'
import { useToast } from '../components/Toast'

type AuthState = {
  authenticated: boolean
  loading: boolean
  model: string
  version: string
  refresh: () => Promise<void>
  login: (password: string) => Promise<void>
  logout: () => Promise<void>
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const toast = useToast()
  const [state, setState] = useState({
    authenticated: false,
    loading: true,
    model: '',
    version: '',
  })

  const refresh = useCallback(async () => {
    // A valid session cookie alone is enough; the stored bearer token is a
    // fallback for contexts where sessionStorage survives but the cookie was
    // already validated once.
    try {
      const data = await api.get<SessionInfo>('/api/v1/dashboard/auth/me')
      setState({ authenticated: true, loading: false, model: data.model, version: data.version })
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        storeToken(null)
        setState((prev) => ({ ...prev, authenticated: false, loading: false }))
        throw error
      }
      setState((prev) => ({ ...prev, loading: false }))
      toast.error(
        '会话状态刷新失败',
        error instanceof ApiError ? error.message : '网络或服务暂时不可用，请稍后重试',
      )
    }
  }, [toast])

  useEffect(() => {
    void refresh().catch(() => undefined)
  }, [refresh])

  useEffect(() => {
    const invalidate = () => {
      setState((prev) => ({ ...prev, authenticated: false, loading: false }))
    }
    window.addEventListener(AUTH_INVALIDATED_EVENT, invalidate)
    return () => window.removeEventListener(AUTH_INVALIDATED_EVENT, invalidate)
  }, [])

  const login = useCallback(async (password: string) => {
    const body = await api.post<{ authenticated: boolean }>('/api/v1/dashboard/auth/login', { password })
    if (!body.authenticated) return
    // Confirm the session before entering. Fetching model/version doubles as that
    // check: the freshly-set cookie is not always readable on the very next
    // request, so a single transient failure must not bounce a real login back to
    // the form — hence the short retry. But if every attempt ends in a definitive
    // 401, the cookie was genuinely not accepted (rejected, stripped by a proxy,
    // or the session invalidated); entering then would only fail on the next call
    // or redirect straight back, so stay unauthenticated and surface the error.
    // Use a direct fetch so it does not trip the global 401-invalidation.
    let session: SessionInfo | null = null
    let sawRejection = false // at least one definitive 401
    let sawTransient = false // at least one network / 5xx failure
    for (let attempt = 0; attempt < 3 && session === null; attempt++) {
      if (attempt > 0) await new Promise((r) => setTimeout(r, 200))
      try {
        const res = await fetch('/api/v1/dashboard/auth/me', {
          credentials: 'same-origin',
          headers: { Accept: 'application/json' },
        })
        if (res.ok) {
          session = ((await res.json()) as { data?: SessionInfo }).data ?? null
        } else if (res.status === 401) {
          sawRejection = true
        } else {
          sawTransient = true // 5xx / other: transient, keep retrying
        }
      } catch {
        sawTransient = true // network error: transient, keep retrying
      }
    }
    // Reject only when the session was never confirmed and every failure was a
    // definitive 401: the cookie was not accepted (rejected, proxy-stripped, or the
    // session invalidated), so entering would only fail on the next call. A single
    // transient failure keeps the optimistic entry the retry loop is there to allow.
    if (session === null && sawRejection && !sawTransient) {
      setState((prev) => ({ ...prev, authenticated: false, loading: false }))
      toast.error('登录会话未能确认', '会话 Cookie 未被接受，请重试或检查反向代理设置')
      return
    }
    setState({
      authenticated: true,
      loading: false,
      model: session?.model ?? '',
      version: session?.version ?? '',
    })
    // Entered without metadata after transient failures; refresh once the session
    // settles so model/version do not stay blank until the next reload.
    if (session === null) void refresh().catch(() => undefined)
  }, [toast, refresh])

  const logout = useCallback(async () => {
    await api.post('/api/v1/dashboard/auth/logout')
    storeToken(null)
    setState((prev) => ({ ...prev, authenticated: false }))
  }, [])

  return <AuthContext.Provider value={{ ...state, refresh, login, logout }}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used inside AuthProvider')
  return ctx
}
