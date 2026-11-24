import { lazy, Suspense } from 'react';
import { money, percent, useResource, endpoint } from './api';
import { Icon } from './Icon';
import { ErrorMessage, Loading } from './ui';
import type { Comparison, Selection, Trends } from './types';

const TrendChart = lazy(() => import('./Charts').then(module => ({ default: module.TrendChart })));
const MarketChart = lazy(() => import('./Charts').then(module => ({ default: module.MarketChart })));

export function PricePanels({ selection, comparisonDate, dates, rangeError, unit }: {
  selection: Selection; comparisonDate: string; dates: string[]; rangeError?: string; unit: string;
}) {
  const trend = useResource<Trends>(rangeError ? null : endpoint('trends', { ...selection }));
  const compare = useResource<Comparison>(rangeError ? null : endpoint('comparisons', {
    run_id: selection.run_id, product_id: selection.product_id, district: selection.district,
    market_id: selection.market_id, quote_date: comparisonDate,
  }));
  const stats = compare.data?.summary;
  const missingMarkets = compare.data?.items.filter(item => item.price == null) ?? [];
  return <>
    <ErrorMessage message={compare.error} />
    <section className="metric-grid" aria-label="所选日期价格摘要" aria-busy={compare.loading}>
      <article className="metric-card">
        <div className="metric-label">本期市场均价<Icon name="market" size={18} /></div>
        <div className="metric-value"><small>¥</small>{money(stats?.mean_price)}</div>
        <p>{unit} · 有效市场等权平均</p>
      </article>
      <article className="metric-card">
        <div className="metric-label">同市场周变化<Icon name="chart" size={18} /></div>
        <div className={`metric-value rate ${(compare.data?.common_market_wow_change ?? 0) > 0 ? 'up' : 'down'}`}>{percent(compare.data?.common_market_wow_change)}</div>
        <p>连续两期配对 {compare.data?.common_market_count ?? '—'} 个市场</p>
      </article>
      <article className="metric-card">
        <div className="metric-label">市场最高最低价差<Icon name="layers" size={18} /></div>
        <div className="metric-value"><small>¥</small>{stats?.ranking_available ? money(stats.price_range) : '—'}</div>
        <p>{stats?.ranking_available ? `${money(stats.min_price)} 至 ${money(stats.max_price)} ${unit}` : '至少两个有效市场时可比较'}</p>
      </article>
      <article className="metric-card">
        <div className="metric-label">有效报价覆盖<Icon name="check" size={18} /></div>
        <div className="metric-value">{stats?.quoted_market_count ?? '—'}<small className="denominator">/ {stats?.expected_market_count ?? '—'}</small></div>
        <p>无报价 {stats?.missing_market_count ?? '—'} · 无记录 {stats?.absent_market_count ?? '—'}</p>
      </article>
    </section>
    <div className="charts-grid">
      <section className="panel trend-panel" id="price-trends">
        <div className="panel-heading"><div><h2>周度价格趋势</h2><p>均价与市场价格区间 · {unit}</p></div><span className="tag">{trend.data?.items.length ?? '—'} 期记录</span></div>
        <ErrorMessage message={trend.error} />
        {rangeError ? <div className="empty-state">请选择有效的日期区间</div> : trend.loading ? <Loading /> : trend.data?.items.length ?
          <Suspense fallback={<Loading text="正在加载图表" />}><TrendChart points={trend.data.items} dates={dates} unit={unit} /></Suspense> :
          <div className="empty-state"><Icon name="chart" size={30} /><strong>这段时间没有该规格的报价记录</strong><span>可以调整日期范围或选择其他规格。</span></div>}
        {trend.data?.items.length ? <details className="data-details"><summary>查看趋势数据表</summary>
          <div className="table-scroll compact"><table><caption className="sr-only">当前筛选范围的周度价格</caption>
            <thead><tr><th>报价日期</th><th>均价</th><th>最低价</th><th>最高价</th><th>有效市场</th></tr></thead>
            <tbody>{trend.data.items.map(item => <tr key={item.quote_date}><td>{item.quote_date}</td><td>{money(item.mean_price)}</td><td>{money(item.min_price)}</td><td>{money(item.max_price)}</td><td>{item.quoted_market_count}/{item.expected_market_count}</td></tr>)}</tbody>
          </table></div></details> : null}
        <div className="panel-note"><Icon name="info" size={15} />缺期保留断点，无报价不补0；图表可缩放查看。</div>
      </section>
      <section className="panel comparison-panel" id="market-comparison">
        <div className="panel-heading"><div><h2>同日市场比价</h2><p>{comparisonDate} · {unit}</p></div><span className="tag">由低到高</span></div>
        {rangeError ? <div className="empty-state">请选择有效的日期区间</div> : compare.loading ? <Loading /> : compare.data && compare.data.summary.quoted_market_count > 0 ?
          <Suspense fallback={<Loading text="正在加载图表" />}><MarketChart comparison={compare.data} /></Suspense> :
          <div className="empty-state"><Icon name="market" size={30} /><strong>所选日期暂无有效报价</strong><span>可调整结束日期，查看该规格的历史报价。</span></div>}
        {missingMarkets.length ? <div className="missing-note"><strong>{missingMarkets.length}个市场暂无价格：</strong>{missingMarkets.map(item => `${item.market}（${item.quote_status === 'missing' ? '无报价' : '无记录'}）`).join('、')}</div> : null}
        {compare.data ? <details className="data-details"><summary>查看市场数据表</summary>
          <div className="table-scroll compact"><table><caption className="sr-only">同日各市场报价和状态</caption>
            <thead><tr><th>市场</th><th>价格</th><th>状态</th></tr></thead>
            <tbody>{compare.data.items.map(item => <tr key={item.market_id}><td>{item.market}</td><td>{money(item.price)}</td><td>{item.quote_status === 'quoted' ? '有报价' : item.quote_status === 'missing' ? '无报价' : '无记录'}</td></tr>)}</tbody>
          </table></div></details> : null}
        <div className="panel-note"><Icon name="info" size={15} />比较限定同规格，价格相同时并列排名。</div>
      </section>
    </div>
  </>;
}
