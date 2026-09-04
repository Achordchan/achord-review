const HEALTH_URL = '/api/v1/dashboard/auth/me'

async function serviceIsUp(): Promise<boolean> {
  try {
    const res = await fetch(HEALTH_URL, { cache: 'no-store', credentials: 'same-origin' })
    return res.status < 500
  } catch {
    return false
  }
}

/**
 * Reload onto the new release, but only once a restart has actually been observed.
 *
 * The old process keeps serving until it exits, so an "up" response before any
 * outage is the *old* frontend — accepting it would reload the stale bundle and
 * stop polling. Recovery is therefore accepted only after at least one failed
 * probe, however long initiation and graceful shutdown take. A final reload on
 * timeout keeps the user from being stranded if the outage is never seen.
 */
export async function waitForServiceThenReload(maxWaitMs = 150_000) {
  const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))
  const started = Date.now()
  let outageObserved = false
  while (Date.now() - started < maxWaitMs) {
    if (await serviceIsUp()) {
      if (outageObserved) {
        window.location.reload()
        return
      }
    } else {
      outageObserved = true
    }
    await sleep(outageObserved ? 2_000 : 1_000)
  }
  window.location.reload()
}
