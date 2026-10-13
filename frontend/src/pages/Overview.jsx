import { ArrowLeftRight, CircleCheck, ShieldAlert, FolderOpen, RefreshCw } from 'lucide-react'
import { api } from '../api/index.js'
import { useApiResource } from '../hooks/useApiResource.js'
import { summaryMetrics, categorySeries, trendSeries } from '../utils/presentation.js'
import { MetricCard } from '../components/MetricCard.jsx'
import { DashboardChartCard } from '../components/DashboardChartCard.jsx'
import { ActivityChart, CategoryChart } from '../components/DashboardCharts.jsx'
import { LoadingState } from '../components/LoadingState.jsx'
import { ErrorState } from '../components/ErrorState.jsx'

const loadSummary = async (signal) => summaryMetrics(await api.dashboard.summary({ signal }))
const loadTrends = async (signal) => trendSeries(await api.dashboard.transactionTrends({}, { signal }))
const loadRisk = async (signal) => categorySeries(await api.dashboard.riskDistribution({ signal }), ['LOW', 'HIGH', 'UNSCORED'])
const loadStatus = async (signal) => categorySeries(await api.dashboard.alertStatus({ signal }), ['OPEN', 'IN_REVIEW', 'CLOSED'])
const icons = [ArrowLeftRight, CircleCheck, ShieldAlert, FolderOpen]

export function Overview() {
  const summary = useApiResource(loadSummary)
  const trends = useApiResource(loadTrends)
  const risk = useApiResource(loadRisk)
  const status = useApiResource(loadStatus)
  const resources = [summary, trends, risk, status]
  const loading = resources.some((item) => item.status === 'loading')
  return <>
    <section className="view-toolbar">
      <div><span className="eyebrow">TRANSACTION INTELLIGENCE</span><h2>A clearer view of transaction risk.</h2><p>Saved SQL records · No live model inference or graph queries</p></div>
      <button className="secondary-button" disabled={loading} onClick={() => resources.forEach((item) => item.refresh())}><RefreshCw size={15} aria-hidden="true" />Refresh dashboard</button>
    </section>
    {summary.status === 'loading' ? <div className="panel resource-state"><LoadingState message="Loading dashboard metrics…" /></div>
      : summary.status === 'error' ? <div className="panel resource-state"><ErrorState message={summary.error} onRetry={summary.refresh} retryLabel="Retry metrics" /></div>
        : <div className="metrics-grid">{summary.data.map((item, index) => <MetricCard key={item.name} {...item} icon={icons[index]} />)}</div>}
    <DashboardChartCard title="Transaction activity" subtitle="Daily transaction volume · Latest stored event window" resource={trends} empty={!trends.data?.length} emptyText="No transaction history">
      {trends.data?.length > 0 && <ActivityChart data={trends.data} />}
    </DashboardChartCard>
    <div className="charts-grid">
      <DashboardChartCard title="Risk distribution" subtitle="Saved classifications · All transactions" resource={risk} empty={!risk.data?.some((item) => item.count > 0)} emptyText="No transactions to classify">
        {risk.data && <CategoryChart data={risk.data} />}
      </DashboardChartCard>
      <DashboardChartCard title="Investigation status" subtitle="Existing alerts · All investigation statuses" resource={status} empty={!status.data?.some((item) => item.count > 0)} emptyText="No investigation alerts">
        {status.data && <CategoryChart data={status.data} />}
      </DashboardChartCard>
    </div>
    <p className="page-note">HIGH flags a saved assessment for review; it is not proof of fraud. LOW is not proof of legitimacy. UNSCORED means no score is saved. Separate dashboard requests may observe different database states.</p>
  </>
}
