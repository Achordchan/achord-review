/**
 * Human GitHub URLs derived from the stored owner/repo + PR number.
 *
 * Reviews record the provider's internal PR URL (api.github.com/...), which is not
 * useful to a person. repo_name ("owner/repo") and pr_number are stored reliably,
 * so we build the graphical github.com links from those instead.
 */
export function repoHtmlUrl(repo: string): string {
  return `https://github.com/${repo}`
}

export function prHtmlUrl(repo: string, prNumber: number): string {
  return `https://github.com/${repo}/pull/${prNumber}`
}
