import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import EChart from 'echarts-for-react/esm/core'
import { api } from '../lib/api'
import type { StatsOverview } from '../lib/types'
import { Card, CardHeader, Skeleton, StatCard } from '../components/ui'
import { formatDuration, formatTokens } from '../lib/format'

echarts.use([LineChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer])

function TrendChart({ trend }: { trend: Record<string, { count: number; tokens: number }> }) {
  // fill a full 14-day window so gaps show as zero, not holes
  const labels: string[] = []
  const counts: number[] = []
  const tokens: number[] = []
  const now = new Date()
  for (let i = 13; i >= 0; i--) {
    const d = new Date(now.getTime() - i * 86_400_000)
    const key = d.toISOString().slice(0, 10)
    labels.push(key.slice(5))
    const bucket = trend[key]
    counts.push(bucket?.count ?? 0)
    tokens.push(bucket?.tokens ?? 0)
  }

  const option = {
    backgroundColor: 'transparent',
    grid: { left: 48, right: 56, top: 32, bottom: 28 },
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#1d2433',
      borderColor: '#263043',
      textStyle: { color: '#e8ecf3', fontSize: 12 },
    },
    legend: {
      data: ['审查次数', 'Token 消耗'],
      textStyle: { color: '#8b95a9', fontSize: 11 },
      top: 0,
      right: 0,
      itemWidth: 14,
    },
    xAxis: {
      type: 'category',
      data: labels,
      axisLine: { lineStyle: { color: '#263043' } },
      axisLabel: { color: '#8b95a9', fontSize: 10 },
      axisTick: { show: false },
    },
    yAxis: [
      {
        type: 'value',
        minInterval: 1,
        axisLabel: { color: '#8b95a9', fontSize: 10 },
        splitLine: { lineStyle: { color: '#1d2433' } },
      },
      {
        type: 'value',
        axisLabel: {
          color: '#8b95a9', fontSize: 10,
          formatter: (v: number) => (v >= 1000 ? `${Math.round(v / 1000)}k` : String(v)),
        },
        splitLine: { show: false },
      },
    ],
    series: [
      {
        name: '审查次数',
        type: 'line',
        data: counts,
        smooth: true,
        symbol: 'circle',
        symbolSize: 5,
        lineStyle: { color: '#6d8dff', width: 2 },
        itemStyle: { color: '#6d8dff' },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(109,141,255,0.25)' },
            { offset: 1, color: 'rgba(109,141,255,0)' },
          ]),
        },
      },
      {
        name: 'Token 消耗',
        type: 'line',
        yAxisIndex: 1,
        data: tokens,
        smooth: true,
        symbol: 'none',
        lineStyle: { color: '#3ecf8e', width: 1.5, type: 'dashed' },
        itemStyle: { color: '#3ecf8e' },
      },
    ],
  }
  return <EChart option={option} echarts={echarts} style={{ height: 260, width: '100%' }} notMerge lazyUpdate />
}

export default function OverviewPage() {
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['stats-overview'],
    queryFn: () => api.get<StatsOverview>('/api/v1/dashboard/stats/overview'),
    refetchInterval: 30_000,
  })

  if (isLoading) {
    return (
      <div className="space-y-5">
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          {Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-28" />)}
        </div>
        <Skeleton className="h-80" />
        <Skeleton className="h-56" />
      </div>
    )
  }

  if (isError || !data) {
    return (
      <Card className="p-10 text-center">
        <p className="text-sm text-muted">统计加载失败</p>
        <button onClick={() => void refetch()} className="mt-3 rounded-lg border border-line px-4 py-2 text-sm text-text hover:bg-surface-2">
          重试
        </button>
      </Card>
    )
  }

  const severityEntries = Object.entries(data.severity_distribution ?? {})
    .filter(([sev]) => ['P0', 'P1', 'P2', 'P3'].includes(sev))
  const severityTotal = severityEntries.reduce((sum, [, n]) => sum + n, 0)

  return (
    <div className="space-y-5">
      <div className="flex items-baseline justify-between">
        <h1 className="text-xl font-semibold text-text">总览大盘</h1>
        <p className="text-xs text-muted">数据截至 {data.generated_for_date}，每 30 秒自动刷新</p>
      </div>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatCard label="累计审查" value={data.total ?? 0} sub={`今日 ${data.today ?? 0} 次`} />
        <StatCard
          label="P0 / P1 拦截"
          value={data.p0_p1_blocked ?? 0}
          sub="累计高危发现"
          accent={data.p0_p1_blocked > 0 ? 'warn' : 'good'}
        />
        <StatCard
          label="失败 / 运行中"
          value={`${data.failed ?? 0} / ${data.running ?? 0}`}
          sub="失败次数 / 当前进行中"
          accent={data.failed > 0 ? 'bad' : 'default'}
        />
        <StatCard
          label="平均耗时"
          value={formatDuration(data.avg_duration_ms ?? 0)}
          sub={`Token 累计 ${formatTokens(data.total_tokens ?? 0)}`}
          accent="info"
        />
      </div>

      <Card>
        <CardHeader title="近 14 天趋势" description="审查次数与 Token 消耗" />
        <div className="p-4">
          {Object.keys(data.daily_trend ?? {}).length === 0 ? (
            <EmptyTrend />
          ) : (
            <TrendChart trend={data.daily_trend} />
          )}
        </div>
      </Card>

      <Card>
        <CardHeader
          title="严重度分布"
          description={`全部审查累计 ${severityTotal} 条发现`}
          action={
            <Link to="/dashboard/reviews" className="text-xs text-accent hover:underline">
              查看审查历史 →
            </Link>
          }
        />
        <div className="space-y-3 p-5">
          {severityTotal === 0 ? (
            <p className="py-6 text-center text-sm text-muted">还没有任何发现记录，干净的仓库就是最好的仓库。</p>
          ) : (
            severityEntries.map(([sev, n]) => {
              const pct = severityTotal > 0 ? Math.max(2, Math.round((n / severityTotal) * 100)) : 0
              const color = sev === 'P0' ? 'bg-bad' : sev === 'P1' ? 'bg-warn' : sev === 'P2' ? 'bg-info' : 'bg-muted'
              return (
                <div key={sev} className="flex items-center gap-3">
                  <span className="w-7 text-xs font-semibold text-muted">{sev}</span>
                  <div className="h-2 flex-1 overflow-hidden rounded-full bg-surface-3">
                    <div className={`h-full rounded-full ${color} transition-all duration-500`} style={{ width: `${pct}%` }} />
                  </div>
                  <span className="w-10 text-right text-xs tabular-nums text-text">{n}</span>
                  <span className="w-10 text-right text-xs tabular-nums text-muted">{pct}%</span>
                </div>
              )
            })
          )}
        </div>
      </Card>
    </div>
  )
}

function EmptyTrend() {
  return (
    <div className="flex flex-col items-center justify-center py-16">
      <div className="flex h-12 w-12 items-center justify-center rounded-full bg-surface-3 text-muted">📈</div>
      <p className="mt-3 text-sm text-muted">还没有审查数据</p>
      <Link
        to="/dashboard/playground"
        className="mt-4 rounded-lg bg-accent-strong px-4 py-2 text-xs font-semibold text-white hover:bg-accent"
      >
        去演练台跑第一次审查 →
      </Link>
    </div>
  )
}
