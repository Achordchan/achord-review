import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

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
        {content}
      </Markdown>
    </div>
  )
}
