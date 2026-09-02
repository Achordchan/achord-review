export class ApiError extends Error {
  status: number
  code?: string
  constructor(status: number, message: string, code?: string) {
    super(message)
    this.status = status
    this.code = code
  }
}

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
    // DOM storage unavailable — the HttpOnly cookie session remains usable.
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = readToken()
  const headers: Record<string, string> = {
    ...(options.headers as Record<string, string> | undefined),
  }
  if (options.body) headers['Content-Type'] = 'application/json'
  if (token) headers['Authorization'] = `Bearer ${token}`
  const response = await fetch(path, { ...options, headers })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail =
      (body as { message?: string; detail?: string }).message ??
      (body as { detail?: string }).detail ??
      `请求失败 (${response.status})`
    throw new ApiError(response.status, detail, (body as { code?: string }).code)
  }
  return (body as { data: T }).data ?? body
}

export const api = {
  get: <T,>(path: string) => request<T>(path),
  post: <T,>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
  put: <T,>(path: string, body?: unknown) =>
    request<T>(path, { method: 'PUT', body: body === undefined ? undefined : JSON.stringify(body) }),
}
