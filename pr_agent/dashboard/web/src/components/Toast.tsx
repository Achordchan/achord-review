import { createContext, useCallback, useContext, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { CheckCircle2, Info, TriangleAlert, X } from 'lucide-react'

type ToastKind = 'success' | 'error' | 'info'
type Toast = { id: number; kind: ToastKind; title: string; detail?: string }

const ToastContext = createContext<{
  success: (title: string, detail?: string) => void
  error: (title: string, detail?: string) => void
  info: (title: string, detail?: string) => void
} | null>(null)

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const nextId = useRef(1)

  const push = useCallback((kind: ToastKind, title: string, detail?: string) => {
    const id = nextId.current++
    setToasts((prev) => [...prev.slice(-4), { id, kind, title, detail }])
    window.setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id))
    }, kind === 'error' ? 8000 : 4000)
  }, [])

  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id))
  }, [])

  const value = useMemo(() => ({
    success: (title: string, detail?: string) => push('success', title, detail),
    error: (title: string, detail?: string) => push('error', title, detail),
    info: (title: string, detail?: string) => push('info', title, detail),
  }), [push])

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="pointer-events-none fixed bottom-5 right-5 z-50 flex w-80 flex-col gap-2">
        {toasts.map((toast) => (
          <div
            key={toast.id}
            role="status"
            className={`animate-fade-in pointer-events-auto flex items-start gap-2.5 rounded-lg border px-3.5 py-3 shadow-lg backdrop-blur ${
              toast.kind === 'success' ? 'border-good/40 bg-surface-2/95' :
              toast.kind === 'error' ? 'border-bad/40 bg-surface-2/95' :
              'border-info/40 bg-surface-2/95'
            }`}
          >
            {toast.kind === 'success' ? <CheckCircle2 size={16} className="mt-0.5 shrink-0 text-good" /> :
             toast.kind === 'error' ? <TriangleAlert size={16} className="mt-0.5 shrink-0 text-bad" /> :
             <Info size={16} className="mt-0.5 shrink-0 text-info" />}
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium text-text">{toast.title}</p>
              {toast.detail && <p className="mt-0.5 break-words text-xs text-muted">{toast.detail}</p>}
            </div>
            <button
              onClick={() => dismiss(toast.id)}
              className="shrink-0 rounded p-0.5 text-muted transition-colors hover:text-text"
              aria-label="关闭"
            >
              <X size={14} />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast() {
  const ctx = useContext(ToastContext)
  if (!ctx) throw new Error('useToast must be used inside ToastProvider')
  return ctx
}
