export type SessionInfo = {
  authenticated: boolean
  model: string
  version: string
}

export type SeverityCounts = Record<string, number>

export type ReviewRow = {
  id: number
  request_id: string
  repo_name: string
  pr_number: number
  pr_title: string | null
  pr_url: string
  commit_sha: string | null
  sender: string | null
  trigger_type: string
  command: string
  status: 'RUNNING' | 'COMPLETED' | 'FAILED' | 'SKIPPED'
  verdict: string | null
  verdict_reason: string | null
  model: string | null
  reasoning_effort: string | null
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  duration_ms: number
  error_message: string | null
  created_at: string
  completed_at: string | null
  severity_counts: SeverityCounts
}

export type ReviewIssue = {
  id: number
  severity: string | null
  relevant_file: string | null
  relevant_lines_start: number | null
  relevant_lines_end: number | null
  issue_summary: string | null
  suggestion: string | null
}

export type ReviewDetail = ReviewRow & {
  raw_prediction: string | null
  markdown_output: string | null
  issues: ReviewIssue[]
}

export type ConfigValues = {
  model: string
  reasoning_effort: string
  api_base: string
  key: string
  extra_instructions: string
  ai_timeout: number | null
  max_model_tokens: number | null
  num_max_findings: number | null
  verdict_blocking_severities: string[]
  ignore_glob: string[]
}

export type ConfigData = {
  available: boolean
  path: string | null
  values: Partial<ConfigValues>
}

export type AuditLogRow = {
  id: number
  operator: string
  action: string
  details_json: string | null
  ip_address: string | null
  created_at: string
}

export type ReviewListData = { total: number; items: ReviewRow[] }
export type AuditLogListData = { items: AuditLogRow[] }
export type RepoRow = { repo_name: string; review_count: number; last_review_at: string | null }
export type OpsTask = { running: boolean; exists: boolean; exit_code: number | null; output: string[] }
export type DiagnoseResult = {
  ok: boolean
  llm: Record<string, unknown>
  github_app: Record<string, unknown>
  storage: Record<string, unknown>
}

export type StatsOverview = {
  total: number
  today: number
  failed: number
  running: number
  avg_duration_ms: number | null
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  p0_p1_blocked: number
  severity_distribution: Record<string, number>
  daily_trend: Record<string, { count: number; tokens: number }>
  generated_for_date: string
}
