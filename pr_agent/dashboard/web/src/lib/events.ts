import { useEffect, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'

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

function readLastEventId(): number {
  try {
    return Number(localStorage.getItem(LAST_EVENT_ID_KEY) ?? '') || 0
  } catch {
    return 0
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
  window.dispatchEvent(new CustomEvent<EventsStatus>(EVENTS_STATUS_EVENT, { detail: status }))
}

/**
 * The single SSE connection for the whole panel, mounted in DashboardLayout.
 *
 * On every dashboard event it (1) invalidates the affected react-query caches
 * and (2) re-dispatches the event for the notification layer. EventSource
 * reconnects on its own with Last-Event-ID; while the connection is down the
 * status event tells data queries to fall back to their polling intervals.
 */
export function useDashboardEvents() {
  const queryClient = useQueryClient()

  useEffect(() => {
    const source = new EventSource(`/api/v1/dashboard/events/stream?lastEventId=${readLastEventId()}`)
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

    return () => {
      source.close()
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
  const [status, setStatus] = useState<EventsStatus>('connecting')
  useEffect(() => onEventsStatus(setStatus), [])
  return status
}
