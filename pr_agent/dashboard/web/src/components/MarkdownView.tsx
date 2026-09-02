import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

/** GitHub-flavored markdown renderer styled to echo the PR comment look. */
export function MarkdownView({ content }: { content: string }) {
  return (
    <div className="md-view">
      <Markdown remarkPlugins={[remarkGfm]}>{content}</Markdown>
    </div>
  )
}
