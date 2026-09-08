import { useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import confetti from 'canvas-confetti'
import { onDashboardEvent } from '../lib/events'
import type { DashboardEvent } from '../lib/events'
import { useToast } from './Toast'

const NOTIFICATION_PREF_KEY = 'dashboard-notifications'

type PermissionState = 'default' | 'granted' | 'denied' | 'unsupported'

// Session fallback when localStorage is blocked: the user granted permission
// via an explicit click, so the preference must survive storage failure for
// the rest of the tab session — otherwise "enabled" silently means "off".
let sessionNotificationsEnabled = false

export function notificationPermission(): PermissionState {
  if (typeof window === 'undefined' || !('Notification' in window)) return 'unsupported'
  return Notification.permission
}

export function notificationsEnabled(): boolean {
  if (notificationPermission() !== 'granted') return false
  try {
    return localStorage.getItem(NOTIFICATION_PREF_KEY) === 'on' || sessionNotificationsEnabled
  } catch {
    return sessionNotificationsEnabled
  }
}

export async function enableNotifications(): Promise<PermissionState> {
  if (!('Notification' in window)) return 'unsupported'
  // requestPermission must come from a user gesture — this is the click handler
  const permission = await Notification.requestPermission()
  if (permission === 'granted') {
    sessionNotificationsEnabled = true
    try {
      localStorage.setItem(NOTIFICATION_PREF_KEY, 'on')
    } catch {
      // preference won't persist across reloads; this session still works
    }
  }
  return permission
}

function notify(title: string, body: string, tag: string, onClick: () => void) {
  if (!notificationsEnabled()) return
  try {
    const notification = new Notification(title, { body, tag })
    notification.onclick = () => {
      window.focus()
      onClick()
      notification.close()
    }
  } catch {
    // a notification failure never breaks the panel
  }
}

function celebrate() {
  // left and right bursts, GitHub-merge style
  void confetti({ particleCount: 90, spread: 70, origin: { x: 0.2, y: 0.7 }, colors: ['#3ecf8e', '#6d8dff', '#e8ecf3'] })
  void confetti({ particleCount: 90, spread: 70, origin: { x: 0.8, y: 0.7 }, colors: ['#3ecf8e', '#6d8dff', '#e8ecf3'] })
}

function eventText(event: DashboardEvent): { title: string; body: string } {
  const repo = event.repo_name || '未知仓库'
  const pr = `PR #${event.pr_number}${event.pr_title ? ` · ${event.pr_title}` : ''}`
  switch (event.event_type) {
    case 'review.requested':
      return { title: `收到新的审查请求 · ${repo}`, body: `${pr}${event.sender ? `（${event.sender} 触发）` : ''}` }
    case 'review.completed': {
      const verdict = event.verdict === 'APPROVE' ? '✅ 审查通过' : event.verdict === 'REQUEST_CHANGES' ? '⚠️ 要求修改' : '审查完成'
      return { title: `${verdict} · ${repo}`, body: pr }
    }
    case 'review.failed':
      return { title: `❌ 审查失败 · ${repo}`, body: pr }
    case 'review.skipped':
      return { title: `审查已跳过 · ${repo}`, body: pr }
    case 'review.reply':
      return { title: `${event.sender || '有人'} 回复了审查的 PR · ${repo}`, body: pr }
    default:
      return { title: `仪表盘事件 · ${repo}`, body: pr }
  }
}

// Events older than this are replayed backlog (a returning tab resuming
// from its saved cursor), not live news: they still reconcile the caches
// (the events hook invalidates queries per event regardless) but stay
// silent — no toasts, no desktop notifications, no confetti. A real review
// completes minutes after its request, so a fresh event is always recent.
const REPLAY_MAX_AGE_MS = 5 * 60_000

function isReplayed(event: DashboardEvent): boolean {
  const createdAt = Date.parse(event.created_at)
  return Number.isFinite(createdAt) && Date.now() - createdAt > REPLAY_MAX_AGE_MS
}

/**
 * Listens to the shared SSE event dispatch and turns review-lifecycle events
 * into toasts, desktop notifications and — when a review passes — confetti.
 * Mount once inside DashboardLayout.
 */
export function useEventNotifications() {
  const toast = useToast()
  const navigate = useNavigate()

  useEffect(() => {
    return onDashboardEvent((event) => {
      if (isReplayed(event)) return
      const { title, body } = eventText(event)
      const detail = event.review_id ? `${body}（点击查看详情）` : body
      const openDetail = event.review_id
        ? () => navigate(`/dashboard/reviews/${event.review_id}`)
        : undefined
      if (event.event_type === 'review.completed') {
        if (event.verdict === 'APPROVE') {
          toast.success(title, detail, openDetail)
          celebrate()
        } else {
          toast.info(title, detail, openDetail)
        }
      } else if (event.event_type === 'review.failed') {
        toast.error(title, detail, openDetail)
      } else {
        toast.info(title, detail, openDetail)
      }
      notify(title, body, `review-${event.request_id || event.id}`,
             openDetail ?? (() => {}))
    })
  }, [toast, navigate])
}
