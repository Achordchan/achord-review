const HEALTH_URL = '/api/v1/dashboard/auth/me'
const PROBE_TIMEOUT_MS = 5_000

async function serviceIsUp(): Promise<boolean> {
  try {
    // Bound every probe: a proxy that accepts the connection during shutdown but
    // never answers would otherwise hang this fetch forever, and the recovery loop
    // awaits it — so maxWaitMs (and the fallback reload) would never be reached.
    const res = await fetch(HEALTH_URL, {
      cache: 'no-store',
      credentials: 'same-origin',
      signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
    })
    return res.status < 500
  } catch {
    return false
  }
}

/** The hashed entry bundle this page was loaded with, e.g. /assets/index-ab12.js. */
function loadedBundle(): string | null {
  const el = document.querySelector<HTMLScriptElement>('script[type="module"][src*="/assets/index-"]')
  const match = el?.getAttribute('src')?.match(/\/assets\/index-[\w-]+\.js/)
  return match ? match[0] : null
}

/** The entry bundle the server currently serves, read from a fresh index.html. */
async function servedBundle(): Promise<string | null> {
  try {
    const res = await fetch('/', {
      cache: 'no-store',
      credentials: 'same-origin',
      signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
    })
    if (!res.ok) return null
    const match = (await res.text()).match(/\/assets\/index-[\w-]+\.js/)
    return match ? match[0] : null
  } catch {
    return null
  }
}

/**
 * Reload onto the new release once a restart is confirmed.
 *
 * Two independent signals confirm it, so neither a slow restart nor a fast one is
 * missed: an observed outage followed by the service coming back, OR the served
 * entry bundle's hash changing from the one this page loaded (a new release booted
 * even if the restart was too fast for any probe to catch an outage). A pure
 * backend restart that leaves the bundle unchanged is caught by the outage path; if
 * it is so fast that no probe sees the outage, the running bundle is already current
 * and nothing needs reloading. A final reload on timeout avoids stranding the user.
 */
export async function waitForServiceThenReload(maxWaitMs = 150_000) {
  const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))
  const started = Date.now()
  const baseline = loadedBundle()
  let outageObserved = false
  while (Date.now() - started < maxWaitMs) {
    if (await serviceIsUp()) {
      if (outageObserved) {
        window.location.reload()
        return
      }
      if (baseline) {
        const served = await servedBundle()
        if (served && served !== baseline) {
          window.location.reload()
          return
        }
      }
    } else {
      outageObserved = true
    }
    await sleep(outageObserved ? 2_000 : 1_000)
  }
  window.location.reload()
}
