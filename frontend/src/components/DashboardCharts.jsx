import { ResponsiveContainer, LineChart, Line, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend, Cell } from 'recharts'
import { formatCount, formatUtcDate } from '../utils/presentation.js'

const colors = { LOW: '#358d7b', HIGH: '#c5843f', UNSCORED: '#9daec1', OPEN: '#4a7caa', IN_REVIEW: '#8872b1', CLOSED: '#358d7b' }
const tooltipStyle = { border: '1px solid #e0e6ee', borderRadius: 8, fontSize: 12, color: '#202d42' }
export function ActivityChart({ data }) {
  return <>
    <div className="chart-canvas">
      <ResponsiveContainer width="100%" height="100%" minWidth={0}>
        <LineChart data={data} margin={{ top: 10, right: 15, bottom: 5, left: 0 }} accessibilityLayer>
          <CartesianGrid stroke="#edf1f6" vertical={false} />
          <XAxis dataKey="date" tickFormatter={(date) => formatUtcDate(date).slice(0, 6)} minTickGap={35} tick={{ fontSize: 10 }} tickLine={false} axisLine={false} />
          <YAxis domain={[0, 'auto']} allowDecimals={false} tickFormatter={formatCount} tick={{ fontSize: 10 }} width={48} tickLine={false} axisLine={false} />
          <Tooltip labelFormatter={formatUtcDate} formatter={(value) => [formatCount(value), 'Transactions']} contentStyle={tooltipStyle} />
          <Legend wrapperStyle={{ fontSize: 11 }} />
          <Line type="linear" dataKey="transaction_count" name="Transactions per UTC day" stroke="#358d7b" strokeWidth={2.5} dot={data.length <= 7} activeDot={{ r: 5 }} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
    <p className="chart-footnote">{formatUtcDate(data[0].date)} – {formatUtcDate(data.at(-1).date)} · UTC event dates</p>
  </>
}
export function CategoryChart({ data }) {
  return <>
    <div className="chart-canvas category-canvas">
      <ResponsiveContainer width="100%" height="100%" minWidth={0}>
        <BarChart data={data} margin={{ top: 10, right: 12, bottom: 5, left: 0 }} accessibilityLayer>
          <CartesianGrid stroke="#edf1f6" vertical={false} />
          <XAxis dataKey="name" tick={{ fontSize: 10 }} tickLine={false} axisLine={false} interval={0} />
          <YAxis domain={[0, 'auto']} allowDecimals={false} tickFormatter={formatCount} tick={{ fontSize: 10 }} width={42} tickLine={false} axisLine={false} />
          <Tooltip formatter={(value) => [formatCount(value), 'Records']} contentStyle={tooltipStyle} cursor={{ fill: '#f3f6fa' }} />
          <Bar dataKey="count" name="Records" maxBarSize={50} radius={[4, 4, 0, 0]} isAnimationActive={false}>
            {data.map((item) => <Cell key={item.name} fill={colors[item.name]} />)}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
    <ul className="category-legend" aria-label="Category counts">{data.map((item) => <li key={item.name}><span style={{ background: colors[item.name] }} aria-hidden="true" />{item.name}<strong>{formatCount(item.count)}</strong></li>)}</ul>
  </>
}
