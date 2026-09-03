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
    if (body.authenticated) {
      setState((prev) => ({ ...prev, authenticated: true, loading: false }))
      await refresh()
    }
  }, [refresh])

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
