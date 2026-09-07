import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

/**
 * Severity badges are raw `<sub><img src=".../badge/P1-orange..."></sub>` so they survive
 * GitHub's raw-HTML blocks (`<details>`, `<tr>`), where a markdown image would be emitted
 * as literal text. `skipHtml` below drops raw HTML wholesale, badge and `alt` included, so
 * rewrite each badge to inline code first — the same shape the backend uses when a provider
 * has no rich markdown. Keeps the severity visible without allowing arbitrary HTML through.
 */
const SEVERITY_BADGE = /<sub><img\s+src="[^"]*\/badge\/(P[0-3])-[^"]*"[^>]*><\/sub>(?:&nbsp;)?\s*/g

function inlineSeverityBadges(content: string): string {
  return content.replace(SEVERITY_BADGE, '`$1` ')
}

/** GitHub-flavored markdown renderer styled to echo the PR comment look. */
export function MarkdownView({ content }: { content: string }) {
  return (
    <div className="md-view">
      <Markdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        components={{
          img: ({ alt }) => (
            <span className="inline-flex rounded border border-line bg-surface-2 px-2 py-1 text-xs text-muted">
              远程图片已屏蔽{alt ? `：${alt}` : ''}
            </span>
          ),
        }}
      >
        {inlineSeverityBadges(content)}
      </Markdown>
    </div>
  )
}
