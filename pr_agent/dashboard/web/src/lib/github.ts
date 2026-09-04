/**
 * Human GitHub URLs for a review, derived from its stored owner/repo, PR number,
 * and the provider's PR URL.
 *
 * Reviews record the provider's internal PR URL (e.g. api.github.com/...), which is
 * not useful to a person. We rebuild the graphical page URL, taking the host from
 * the stored PR URL so GitHub Enterprise deployments keep their own host — only the
 * public api.github.com host is rewritten to github.com.
 */
function htmlOrigin(prUrl: string): string {
  try {
    const url = new URL(prUrl)
    if (url.hostname === 'api.github.com') return 'https://github.com'
    // GitHub Enterprise serves its API and web UI from the same host
    // (host/api/v3/... vs host/owner/repo), so the origin is already correct.
    return url.origin
  } catch {
    return 'https://github.com'
  }
}

export function repoHtmlUrl(prUrl: string, repo: string): string {
  return `${htmlOrigin(prUrl)}/${repo}`
}

export function prHtmlUrl(prUrl: string, repo: string, prNumber: number): string {
  return `${htmlOrigin(prUrl)}/${repo}/pull/${prNumber}`
}
