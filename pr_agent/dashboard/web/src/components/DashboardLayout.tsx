import { useState } from 'react'
import type { ReactNode } from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import {
  Activity, Beaker, FlaskConical, GitPullRequestArrow, LayoutDashboard,
  LogOut, Settings, ShieldCheck, TerminalSquare, X,
} from 'lucide-react'
import { useAuth } from '../lib/auth'
import { ComingSoonBadge } from './badges'

type NavItem = {
  to: string
  label: string
  icon: ReactNode
  /** "coming_soon" entries render greyed with a badge and open a plan card. */
  phase?: string
  description?: string
}

const NAV_ITEMS: NavItem[] = [
  { to: '/dashboard', label: '总览大盘', icon: <LayoutDashboard size={16} /> },
  { to: '/dashboard/reviews', label: '审查历史', icon: <GitPullRequestArrow size={16} /> },
  { to: '/dashboard/config', label: '配置中心', icon: <Settings size={16} /> },
  { to: '/dashboard/ops', label: '一键运维', icon: <TerminalSquare size={16} /> },
  { to: '/dashboard/playground', label: '演练台', icon: <FlaskConical size={16} /> },
  {
    to: '/dashboard/commands', label: '命令中心', icon: <Beaker size={16} />,
    phase: 'Phase 3',
    description: '释放 PR-Agent 全部引擎能力：在面板上对任意 PR 执行 /describe、/improve、/ask、/update_changelog 等命令，不再局限于 /review。',
  },
  {
    to: '/dashboard/issues', label: '缺陷追踪', icon: <Activity size={16} />,
    phase: 'Phase 3',
    description: '把每个 Finding 变成可管理实体：待处理 / 已修复 / 已驳回 / 已豁免，跨 re-review 自动关联同源缺陷，形成从发现到关闭的完整时间线。',
  },
  {
    to: '/dashboard/repos', label: '仓库管理', icon: <ShieldCheck size={16} />,
    phase: 'Phase 2',
    description: '展示 GitHub App 已接入的全部仓库，按仓库启停自动审查，并为每个仓库配置独立的模型、门禁与 ignore 规则。',
  },
  {
    to: '/dashboard/alerts', label: '告警通知', icon: <Activity size={16} />,
    phase: 'Phase 2',
    description: '审查失败、P0 拦截、Token 异常突增时，通过 Telegram / 企业微信 / 邮件主动推送，不再需要主动去翻 PR 评论。',
  },
]

function ComingSoonCard({ item, onClose }: { item: NavItem; onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={onClose} />
      <div className="animate-fade-in relative w-full max-w-md rounded-xl border border-line bg-surface-1 p-6 shadow-2xl">
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-center gap-2.5">
            <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-surface-3 text-muted">
              {item.icon}
            </span>
            <div>
              <h3 className="text-base font-semibold text-text">{item.label}</h3>
              <p className="text-xs text-muted">规划中的功能模块</p>
            </div>
          </div>
          <button onClick={onClose} className="rounded-md p-1 text-muted hover:text-text" aria-label="关闭">
            <X size={16} />
          </button>
        </div>
        <p className="mt-4 text-sm leading-relaxed text-muted">{item.description}</p>
        <div className="mt-5 rounded-lg border border-dashed border-line bg-surface-2/60 p-4 text-center text-xs text-muted">
          功能界面规划中 · 截图占位
        </div>
        <div className="mt-5 flex items-center justify-between">
          <ComingSoonBadge phase={item.phase ?? ''} />
          <button
            onClick={onClose}
            className="rounded-lg border border-line px-4 py-2 text-sm font-medium text-muted transition-colors hover:bg-surface-2 hover:text-text"
          >
            上线后通知我
          </button>
        </div>
      </div>
    </div>
  )
}

export default function DashboardLayout() {
  const { model, version, logout } = useAuth()
  const navigate = useNavigate()
  const [pendingItem, setPendingItem] = useState<NavItem | null>(null)

  const handleNavClick = (item: NavItem) => {
    if (item.phase) {
      setPendingItem(item)
      return
    }
    navigate(item.to)
  }

  return (
    <div className="flex h-full">
      {/* sidebar */}
      <aside className="flex w-60 shrink-0 flex-col border-r border-line bg-surface-1">
        <div className="flex items-center gap-2.5 px-5 py-5">
          <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-accent to-accent-strong text-sm font-bold text-white">
            A
          </span>
          <div>
            <p className="text-sm font-semibold leading-tight text-text">achord-review</p>
            <p className="text-[11px] leading-tight text-muted">控制面板 v{version}</p>
          </div>
        </div>
        <nav className="min-h-0 flex-1 space-y-0.5 overflow-y-auto px-3 py-2">
          {NAV_ITEMS.map((item) =>
            item.phase ? (
              <button
                key={item.to}
                onClick={() => handleNavClick(item)}
                className="group flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left text-sm text-muted opacity-50 transition-all hover:bg-surface-2 hover:opacity-80"
              >
                <span className="shrink-0">{item.icon}</span>
                <span className="flex-1">{item.label}</span>
                <span className="rounded border border-dashed border-muted/40 px-1 py-px text-[10px] text-muted">
                  {item.phase}
                </span>
              </button>
            ) : (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === '/dashboard'}
                className={({ isActive }) =>
                  `flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm transition-all ${
                    isActive
                      ? 'bg-accent/15 font-medium text-accent'
                      : 'text-muted hover:bg-surface-2 hover:text-text'
                  }`
                }
              >
                <span className="shrink-0">{item.icon}</span>
                <span className="flex-1">{item.label}</span>
              </NavLink>
            ),
          )}
        </nav>
        <div className="border-t border-line p-3">
          <button
            onClick={() => void logout()}
            className="flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-sm text-muted transition-colors hover:bg-surface-2 hover:text-text"
          >
            <LogOut size={16} />
            退出登录
          </button>
        </div>
      </aside>

      {/* main */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 shrink-0 items-center justify-between border-b border-line bg-surface-1/80 px-6 backdrop-blur">
          <div className="flex items-center gap-2 text-sm text-muted">
            <span className="h-2 w-2 rounded-full bg-good" />
            服务运行中
          </div>
          {model && (
            <span className="inline-flex items-center gap-1.5 rounded-md border border-line bg-surface-2 px-2.5 py-1 text-xs text-muted">
              <span className="h-1.5 w-1.5 rounded-full bg-accent" />
              {model.replace(/^openai\//, '')}
            </span>
          )}
        </header>
        <main className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto max-w-6xl animate-fade-in p-6">
            <Outlet />
          </div>
        </main>
      </div>

      {pendingItem && <ComingSoonCard item={pendingItem} onClose={() => setPendingItem(null)} />}
    </div>
  )
}
