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
const LEASE_KEY = 'dashboard-stream-leader'
// One tab (the lease holder) owns the SSE connection; the others receive its
// events over a BroadcastChannel. Without this, every tab holds its own
// hour-long stream and six tabs exhaust the browser's per-origin HTTP/1.1
// connection limit — dashboard API calls then queue behind the streams.
const CHANNEL_NAME = 'dashboard-events-stream'
const LEASE_TTL_MS = 8000
const LEASE_HEARTBEAT_MS = 3000
export const EVENTS_STATUS_EVENT = 'dashboard:events-status'
export const DASHBOARD_EVENT = 'dashboard:event'

type ChannelMessage =
  | { kind: 'event'; event: DashboardEvent }
  | { kind: 'status'; status: EventsStatus }

type LeaderLease = { id: string; ts: number }

// The live status is module-scoped, not hook-scoped: a subscriber mounted
// after the connection already opened (page navigation within the panel)
// must initialize from the current value, not wait for the next transition.
let sharedStatus: EventsStatus = 'connecting'

function readLastEventId(): number | null {
  try {
    const raw = localStorage.getItem(LAST_EVENT_ID_KEY)
    if (raw === null) return null
    const value = Number(raw)
    // a stored 0 is a real position (subscription established against an
    // empty stream) and must not read as "never stored"
    return Number.isFinite(value) && value >= 0 ? value : null
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

function readLease(): LeaderLease | null {
  try {
    const raw = localStorage.getItem(LEASE_KEY)
    if (raw === null) return null
    const lease = JSON.parse(raw) as LeaderLease
    return typeof lease.id === 'string' && Number.isFinite(lease.ts) ? lease : null
  } catch {
    return null
  }
}

function writeLease(lease: LeaderLease) {
  try {
    localStorage.setItem(LEASE_KEY, JSON.stringify(lease))
  } catch {
    // storage blocked: leadership still works per-tab via the in-memory flag,
    // the lease simply cannot be seen by other tabs
  }
}

function clearLease(tabId: string) {
  const lease = readLease()
  if (lease?.id === tabId) {
    try {
      localStorage.removeItem(LEASE_KEY)
    } catch {
      // nothing to clean up
    }
  }
}

export function dispatchEventsStatus(status: EventsStatus) {
  sharedStatus = status
  window.dispatchEvent(new CustomEvent<EventsStatus>(EVENTS_STATUS_EVENT, { detail: status }))
}

export function currentEventsStatus(): EventsStatus {
  return sharedStatus
}

function randomTabId(): string {
  try {
    return crypto.randomUUID()
  } catch {
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
  }
}

/**
 * The panel's event stream, mounted once in DashboardLayout.
 *
 * Cross-tab model: exactly one tab — the lease holder — holds the SSE
 * connection and re-broadcasts every event over a BroadcastChannel; the
 * others stay fully functional (instant cache invalidation, notifications,
 * confetti) without their own connection. If the holder closes or stalls,
 * another tab's takeover check acquires the expired lease within LEASE_TTL.
 *
 * Within the holder tab: a fresh subscription resolves the stream head first
 * so retained history is never replayed as notifications; a saved cursor
 * (reconnect after sleep, reload, or leadership takeover) resumes exactly
 * where it stopped. The connection's cursor lives in memory (advanced per
 * frame, persisted on connect and per frame) — localStorage only bootstraps
 * a brand-new subscription.
 */
export function useDashboardEvents() {
  const queryClient = useQueryClient()

  useEffect(() => {
    let source: EventSource | null = null
    let closed = false
    let leader = false
    let attempt = 0
    let retryTimer: number | null = null
    let leaseTimer: number | null = null
    const tabId = randomTabId()

    let channel: BroadcastChannel | null = null
    try {
      channel = new BroadcastChannel(CHANNEL_NAME)
    } catch {
      channel = null // very old browsers: single-tab mode, still functional
    }

    // The connection's authoritative cursor (leader only). localStorage is
    // only a bootstrap for a brand-new subscription: it is written by the
    // leader on connect and per frame, and never read back mid-connection —
    // a sibling tab's value must not advance this stream past missed events.
    let cursor: number | null = null
    // Consecutive connection failures with a healthy head lookup. Native
    // EventSource cannot send an Authorization header, so a pure bearer
    // session (no usable cookie) fails the stream while the head query
    // succeeds — the counter escalates the delay; retries never stop, so a
    // transient proxy failure that heals also recovers on its own.
    let connectFailures = 0
    const STREAM_RETRY_FAST = 1000
    const STREAM_RETRY_SLOW = 60_000
    const FAST_FAILURES_BEFORE_SLOW = 3

    // Invalidation debounce: an event replay backlog can deliver hundreds of
    // events per poll tick, and each invalidation restarts active refetches —
    // unbatched that is a request storm. Coalesce them into one invalidation
    // pass per burst; cursor advancement, notifications and confetti stay
    // per-event.
    let invalidateTimer: number | null = null
    const invalidateSoon = () => {
      if (invalidateTimer !== null) return
      invalidateTimer = window.setTimeout(() => {
        invalidateTimer = null
        queryClient.invalidateQueries({ queryKey: ['reviews'] })
        queryClient.invalidateQueries({ queryKey: ['review-detail'] })
        queryClient.invalidateQueries({ queryKey: ['review-logs'] })
        queryClient.invalidateQueries({ queryKey: ['stats-overview'] })
      }, 100)
    }

    /** Every frame, however it arrives (own stream or leader broadcast). */
    const handleFrame = (event: DashboardEvent) => {
      invalidateSoon()
      window.dispatchEvent(new CustomEvent<DashboardEvent>(DASHBOARD_EVENT, { detail: event }))
    }

    /** Status the whole tab should see; the leader also broadcasts it. */
    const publishStatus = (status: EventsStatus) => {
      dispatchEventsStatus(status)
      channel?.postMessage({ kind: 'status', status } satisfies ChannelMessage)
    }

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

    const stopStream = () => {
      if (retryTimer !== null) window.clearTimeout(retryTimer)
      retryTimer = null
      if (invalidateTimer !== null) window.clearTimeout(invalidateTimer)
      invalidateTimer = null
      source?.close()
      source = null
      cursor = null
      connectFailures = 0
    }

    const connect = (fromId: number) => {
      cursor = fromId
      // Persist the validated starting position immediately: if the tab
      // closes before any event arrives, a returning subscription resumes
      // from here and still sees everything that happened in between,
      // instead of jumping to the (new) head and skipping it silently.
      storeLastEventId(fromId)
      source = new EventSource(`/api/v1/dashboard/events/stream?lastEventId=${fromId}`)
      publishStatus('connecting')
      source.addEventListener('open', () => {
        connectFailures = 0
        publishStatus('live')
      })
      source.addEventListener('dashboard', (raw) => {
        const frame = raw as MessageEvent<string>
        let event: DashboardEvent
        try {
          event = JSON.parse(frame.data) as DashboardEvent
        } catch {
          return
        }
        cursor = event.id
        storeLastEventId(event.id)
        handleFrame(event)
        channel?.postMessage({ kind: 'event', event } satisfies ChannelMessage)
      })
      source.addEventListener('error', () => {
        // EventSource auto-reconnects, but its Last-Event-ID is whatever the
        // stream last sent: if the database was recreated or restored behind
        // our back, that cursor is too high and the server would silently
        // filter every new event while the connection looks healthy. Take
        // reconnection into our own hands: close, re-validate the cursor
        // against the current head, reconnect from there.
        source?.close()
        source = null
        publishStatus('polling')
        connectFailures += 1
        // Escalate rather than stop: a bearer-only session (EventSource
        // cannot send the token) stays near-polling frequency with a
        // once-a-minute probe instead of a per-second storm, while a
        // transient failure heals and the next open resets the counter.
        const delay = connectFailures >= FAST_FAILURES_BEFORE_SLOW
          ? STREAM_RETRY_SLOW
          : STREAM_RETRY_FAST
        scheduleRetry(delay, true)
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
          if (closed || !leader) return
          const head = Math.max(0, data.last_event_id ?? 0)
          const saved = cursor ?? readLastEventId()
          const fromId = saved === null || saved > head ? head : saved
          connect(fromId)
        })
        .catch(() => {
          if (closed || !leader) return
          // A failed head lookup must NOT fall back to connecting blindly —
          // cursor 0 would replay retained history, a stale cursor may be
          // ahead of a rebuilt database. Keep retrying with capped backoff
          // while polling, and give up never — cleanup cancels the timer.
          publishStatus('polling')
          attempt += 1
          scheduleRetry(Math.min(30_000, attempt * 2000), false)
        })
    }

    // followers receive the leader's frames and status verbatim
    if (channel) {
      channel.onmessage = (raw: MessageEvent<ChannelMessage>) => {
        const message = raw.data
        if (!message || typeof message !== 'object') return
        if (message.kind === 'event') {
          // a frame proves the leader's stream is alive even if this tab
          // joined after the last status broadcast
          if (!leader) {
            dispatchEventsStatus('live')
            handleFrame(message.event)
          }
        } else if (message.kind === 'status') {
          if (!leader) dispatchEventsStatus(message.status)
        }
      }
    }

    // ---- leadership --------------------------------------------------

    /**
     * Acquire or refresh the stream lease. Own lease refreshes in place; a
     * foreign live lease loses; a stale one is taken over. Concurrent
     * acquisitions converge because the lowest tab id wins on re-read.
     */
    const tryAcquireLease = (): boolean => {
      const now = Date.now()
      const lease = readLease()
      if (lease && lease.id !== tabId && now - lease.ts < LEASE_TTL_MS) return false
      writeLease({ id: tabId, ts: now })
      const after = readLease()
      if (after && after.id !== tabId && after.id < tabId) return false // lower id wins
      return true
    }

    const stopLeading = () => {
      leader = false
      if (leaseTimer !== null) window.clearInterval(leaseTimer)
      leaseTimer = null
      stopStream()
      clearLease(tabId)
    }

    const becomeLeader = () => {
      if (leader || closed) return
      leader = true
      dispatchEventsStatus('connecting')
      resolveHead()
      if (!channel) return // single-tab fallback: no lease to defend
      leaseTimer = window.setInterval(() => {
        if (closed || !leader) return
        if (!tryAcquireLease()) {
          // another tab took over (lower id); stop streaming, become follower
          leader = false
          if (leaseTimer !== null) window.clearInterval(leaseTimer)
          leaseTimer = null
          stopStream()
          dispatchEventsStatus('polling')
          return
        }
        // re-announce the status with every lease heartbeat, so a tab that
        // joined between transitions learns the stream state within one beat
        channel.postMessage({ kind: 'status', status: currentEventsStatus() } satisfies ChannelMessage)
      }, LEASE_HEARTBEAT_MS)
    }

    const takeoverCheck = () => {
      if (closed || leader) return
      // without a channel a lease holder cannot share its events; every tab
      // streams for itself instead of honoring a lease it cannot benefit from
      if (!channel) {
        becomeLeader()
        return
      }
      const lease = readLease()
      if (!lease || lease.id === tabId || Date.now() - lease.ts >= LEASE_TTL_MS) {
        if (tryAcquireLease()) becomeLeader()
      }
    }

    const followerTimer = window.setInterval(takeoverCheck, LEASE_HEARTBEAT_MS)
    // bfcache: stop streaming and release the lease on hide, re-acquire on
    // show — otherwise restoration stacks a second stream on live callbacks
    const onLeave = () => {
      if (leader) stopLeading()
    }
    const onReturn = () => {
      takeoverCheck()
    }
    window.addEventListener('pagehide', onLeave)
    window.addEventListener('pageshow', onReturn)

    // first attempt: become the leader right away when the lease is free
    takeoverCheck()

    return () => {
      closed = true
      window.removeEventListener('pagehide', onLeave)
      window.removeEventListener('pageshow', onReturn)
      window.clearInterval(followerTimer)
      if (leaseTimer !== null) window.clearInterval(leaseTimer)
      stopStream()
      clearLease(tabId)
      if (channel) channel.close()
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
