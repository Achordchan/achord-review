import { useEffect, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from './api'

export type DashboardEvent = {
  id: number
  event_type: 'review.requested' | 'review.completed' | 'review.failed' | 'review.skipped' | 'review.reply'
  request_id: string
  review_id: number | null
  repo_name: string
  pr_number: number
  pr_title: string
  sender: string
  status: string
  verdict: string
  created_at: string
}

export type EventsStatus = 'connecting' | 'live' | 'polling'

const LAST_EVENT_ID_KEY = 'dashboard-last-event-id'
export const EVENTS_STATUS_EVENT = 'dashboard:events-status'
export const DASHBOARD_EVENT = 'dashboard:event'

// The live status is module-scoped, not hook-scoped: a subscriber mounted
// after the connection already opened (page navigation within the panel)
// must initialize from the current value, not wait for the next transition.
let sharedStatus: EventsStatus = 'connecting'

function readLastEventId(): number | null {
  try {
    const raw = localStorage.getItem(LAST_EVENT_ID_KEY)
    if (raw === null) return null
    const value = Number(raw)
    return Number.isFinite(value) && value > 0 ? value : null
  } catch {
    return null
  }
}

function storeLastEventId(id: number) {
  try {
    localStorage.setItem(LAST_EVENT_ID_KEY, String(id))
  } catch {
    // resume position simply won't persist across reloads
  }
}

export function dispatchEventsStatus(status: EventsStatus) {
  sharedStatus = status
  window.dispatchEvent(new CustomEvent<EventsStatus>(EVENTS_STATUS_EVENT, { detail: status }))
}

export function currentEventsStatus(): EventsStatus {
  return sharedStatus
}

/**
 * The single SSE connection for the whole panel, mounted in DashboardLayout.
 *
 * A fresh subscriber (no saved cursor) starts at the stream's current head
 * via /events/head so retained history is never replayed as notifications;
 * a saved cursor (reconnect after sleep, reload) resumes exactly where it
 * stopped. On every dashboard event the hook (1) invalidates the affected
 * react-query caches and (2) re-dispatches the event for the notification
 * layer. EventSource reconnects on its own with Last-Event-ID; while the
 * connection is down the status signal tells data queries to fall back to
 * their polling intervals.
 */
export function useDashboardEvents() {
  const queryClient = useQueryClient()

  useEffect(() => {
    let source: EventSource | null = null
    let closed = false

    const connect = (fromId: number) => {
      source = new EventSource(`/api/v1/dashboard/events/stream?lastEventId=${fromId}`)
      dispatchEventsStatus('connecting')
      source.addEventListener('open', () => dispatchEventsStatus('live'))
      source.addEventListener('dashboard', (raw) => {
        dispatchEventsStatus('live')
        const frame = raw as MessageEvent<string>
        let event: DashboardEvent
        try {
          event = JSON.parse(frame.data) as DashboardEvent
        } catch {
          return
        }
        storeLastEventId(event.id)
        queryClient.invalidateQueries({ queryKey: ['reviews'] })
        queryClient.invalidateQueries({ queryKey: ['review-detail'] })
        queryClient.invalidateQueries({ queryKey: ['review-logs'] })
        queryClient.invalidateQueries({ queryKey: ['stats-overview'] })
        window.dispatchEvent(new CustomEvent<DashboardEvent>(DASHBOARD_EVENT, { detail: event }))
      })
      source.addEventListener('error', () => {
        // EventSource auto-reconnects; until it does, queries poll instead
        dispatchEventsStatus('polling')
      })
    }

    const saved = readLastEventId()
    if (saved !== null) {
      connect(saved)
    } else {
      // fresh subscription: skip history, start at the current head. A failed
      // head lookup must NOT fall back to 0 — that would replay every retained
      // event as notifications. Retry with backoff, and give up into polling
      // mode (the status signal keeps queries polling) rather than subscribe.
      let attempt = 0
      const resolveHead = () => {
        api.get<{ last_event_id: number }>('/api/v1/dashboard/events/head')
          .then((data) => {
            if (!closed) connect(data.last_event_id ?? 0)
          })
          .catch(() => {
            if (closed) return
            if (attempt < 3) {
              attempt += 1
              window.setTimeout(resolveHead, attempt * 2000)
            } else {
              dispatchEventsStatus('polling')
            }
          })
      }
      resolveHead()
    }

    return () => {
      closed = true
      source?.close()
      dispatchEventsStatus('polling')
    }
  }, [queryClient])
}

export function onDashboardEvent(handler: (event: DashboardEvent) => void) {
  const listener = (raw: Event) => handler((raw as CustomEvent<DashboardEvent>).detail)
  window.addEventListener(DASHBOARD_EVENT, listener)
  return () => window.removeEventListener(DASHBOARD_EVENT, listener)
}

export function onEventsStatus(handler: (status: EventsStatus) => void) {
  const listener = (raw: Event) => handler((raw as CustomEvent<EventsStatus>).detail)
  window.addEventListener(EVENTS_STATUS_EVENT, listener)
  return () => window.removeEventListener(EVENTS_STATUS_EVENT, listener)
}

export function useEventsStatus(): EventsStatus {
  const [status, setStatus] = useState<EventsStatus>(sharedStatus)
  useEffect(() => onEventsStatus(setStatus), [])
  return status
}
