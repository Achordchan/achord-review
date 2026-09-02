import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { api } from './api'
import type { SessionInfo } from './types'

const TOKEN_KEY = 'dashboard_token'

export function readToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY)
  } catch {
    return null
  }
}

export function storeToken(token: string | null) {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token)
    else sessionStorage.removeItem(TOKEN_KEY)
  } catch {
    // storage unavailable (private mode) — cookie session still works
  }
}

type AuthState = {
  authenticated: boolean
  loading: boolean
  model: string
  version: string
  login: (password: string) => Promise<void>
  logout: () => Promise<void>
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
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
    } catch {
      storeToken(null)
      setState((prev) => ({ ...prev, authenticated: false, loading: false }))
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const login = useCallback(async (password: string) => {
    const body = await api.post<{ authenticated: boolean }>('/api/v1/dashboard/auth/login', { password })
    if (body.authenticated) {
      await refresh()
    }
  }, [refresh])

  const logout = useCallback(async () => {
    try {
      await api.post('/api/v1/dashboard/auth/logout')
    } finally {
      storeToken(null)
      setState((prev) => ({ ...prev, authenticated: false }))
    }
  }, [])

  return <AuthContext.Provider value={{ ...state, login, logout }}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used inside AuthProvider')
  return ctx
}
