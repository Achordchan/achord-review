/**
 * Human GitHub URLs for a review, derived from its stored owner/repo, PR number,
 * and the provider's PR URL.
 *
 * Reviews record the provider's internal PR URL (e.g. api.github.com/...), which is
 * not useful to a person. We rebuild the graphical page URL, deriving the web host
 * from the stored PR URL so GitHub Enterprise deployments keep their own host.
 */
function htmlOrigin(prUrl: string): string {
  try {
    const url = new URL(prUrl)
    // Public GitHub (api.github.com) and GHE.com data-residency tenants
    // (api.<tenant>.ghe.com) both expose the API on an `api.` host distinct from
    // the web host (github.com, <tenant>.ghe.com); strip that prefix to reach the
    // web origin. Restrict the rewrite to these known patterns: a GHE Server host
    // may itself be named `api.<something>` while serving its API under /api/v3 on
    // that very host, and stripping the prefix there would point links at a
    // different, unrelated host.
    if (url.hostname === 'api.github.com') return 'https://github.com'
    if (/^api\..+\.ghe\.com$/.test(url.hostname)) return `${url.protocol}//${url.host.slice(4)}`
    // GitHub Enterprise Server serves its API under a path on the same host
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
