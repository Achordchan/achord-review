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
    let attempt = 0
    let retryTimer: number | null = null
    // The connection's authoritative cursor. localStorage is only a
    // bootstrap for a brand-new subscription: it is written opportunistically
    // per frame but never read back here — writes can fail, and multiple
    // dashboard tabs share one key, so a sibling tab's cursor must not
    // advance this tab past events it has not received.
    let cursor: number | null = null

    const scheduleRetry = (delayMs: number, resetBackoff: boolean) => {
      if (retryTimer !== null) window.clearTimeout(retryTimer)
      if (resetBackoff) attempt = 0
      retryTimer = window.setTimeout(() => {
        // clear the reference BEFORE resolving, so a stream that recovers
        // now can schedule its own reconnection when it later drops
        retryTimer = null
        resolveHead()
      }, delayMs)
    }

    const connect = (fromId: number) => {
      cursor = fromId
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
        cursor = event.id
        storeLastEventId(event.id)
        queryClient.invalidateQueries({ queryKey: ['reviews'] })
        queryClient.invalidateQueries({ queryKey: ['review-detail'] })
        queryClient.invalidateQueries({ queryKey: ['review-logs'] })
        queryClient.invalidateQueries({ queryKey: ['stats-overview'] })
        window.dispatchEvent(new CustomEvent<DashboardEvent>(DASHBOARD_EVENT, { detail: event }))
      })
      source.addEventListener('error', () => {
        // EventSource auto-reconnects, but its Last-Event-ID is whatever the
        // stream last sent: if the database was recreated or restored behind
        // our back, that cursor is too high and the server would silently
        // filter every new event while the connection looks healthy. Take
        // reconnection into our own hands: close, re-validate the cursor
        // against the current head, reconnect from there. Until it succeeds
        // the status signal keeps queries polling.
        source?.close()
        source = null
        dispatchEventsStatus('polling')
        scheduleRetry(1000, true)
      })
    }

    // Every subscription resolves the head first: a fresh browser starts
    // there (never replaying retained history), and the saved cursor is
    // validated against it — a database recreated or restored from an older
    // backup restarts the sequence lower, and a stale-high cursor would
    // silently filter out every new event until the sequence caught up.
    // Reconnection re-resolves with the in-memory cursor this connection
    // actually reached; events created during an outage are picked up from
    // there, not skipped by jumping to the new head.
    const resolveHead = () => {
      api.get<{ last_event_id: number }>('/api/v1/dashboard/events/head')
        .then((data) => {
          if (closed) return
          const head = Math.max(0, data.last_event_id ?? 0)
          const saved = cursor ?? readLastEventId()
          const fromId = saved === null || saved > head ? head : saved
          connect(fromId)
        })
        .catch(() => {
          if (closed) return
          // A failed head lookup must NOT fall back to connecting blindly —
          // cursor 0 would replay retained history, a stale cursor may be
          // ahead of a rebuilt database. Keep retrying with capped backoff
          // while polling, and give up never — cleanup cancels the timer.
          dispatchEventsStatus('polling')
          attempt += 1
          scheduleRetry(Math.min(30_000, attempt * 2000), false)
        })
    }
    resolveHead()

    return () => {
      closed = true
      if (retryTimer !== null) window.clearTimeout(retryTimer)
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
