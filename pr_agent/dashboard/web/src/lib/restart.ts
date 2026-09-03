const HEALTH_URL = '/api/v1/dashboard/auth/me'

async function serviceIsUp(): Promise<boolean> {
  try {
    const res = await fetch(HEALTH_URL, { cache: 'no-store', credentials: 'same-origin' })
    return res.status < 500
  } catch {
    return false
  }
}

/** Observe shutdown before accepting recovery, then reload the new release. */
export async function waitForServiceThenReload(maxWaitMs = 150_000) {
  const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))
  const started = Date.now()
  const outageDeadline = Date.now() + 30_000
  while (Date.now() < outageDeadline) {
    if (!(await serviceIsUp())) break
    await sleep(1_000)
  }
  while (Date.now() - started < maxWaitMs) {
    if (await serviceIsUp()) {
      window.location.reload()
      return
    }
    await sleep(2_000)
  }
  window.location.reload()
}
