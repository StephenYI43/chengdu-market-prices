import { useState } from 'react';
import { endpoint, flagNames, useResource } from './api';
import { Icon } from './Icon';
import { ErrorMessage, Loading } from './ui';
import type { Prices, Selection } from './types';

export function PriceRecords({ selection, rangeError, unit, chooseMarket }: {
  selection: Selection; rangeError?: string; unit: string; chooseMarket: (district: string, marketId: string) => void;
}) {
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState('');
  const [reviewOnly, setReviewOnly] = useState(false);
  const [sort, setSort] = useState('date_desc');
  const filters = { ...selection, quote_status: status, review_only: reviewOnly, sort };
  const records = useResource<Prices>(rangeError ? null : endpoint('prices', { ...filters, page, page_size: 20 }));
  const pages = records.data ? Math.max(1, Math.ceil(records.data.total / 20)) : null;
  return <section className="panel records-panel" id="price-records">
    <div className="panel-heading"><div><h2>原始报价明细 <span className="count-badge">{records.data?.total.toLocaleString() ?? '—'}</span></h2><p>保留原始报价与来源记录，可导出当前筛选的全部结果。</p></div>
      <a className={`button${rangeError || !records.data?.total ? ' disabled' : ''}`} aria-disabled={Boolean(rangeError || !records.data?.total)} href={rangeError || !records.data?.total ? undefined : endpoint('prices/export.csv', filters)}><Icon name="download" size={17} />导出 CSV</a>
    </div>
    <div className="table-toolbar"><div className="table-filter-group">
      <label>报价状态<select value={status} onChange={event => { setStatus(event.target.value); setPage(1); }}><option value="">全部状态</option><option value="quoted">有报价</option><option value="missing">无报价</option></select></label>
      <label className="checkbox-label"><input type="checkbox" checked={reviewOnly} onChange={event => { setReviewOnly(event.target.checked); setPage(1); }} />仅看待复核记录</label>
    </div><label className="sort-label">排序<select aria-label="明细排序" value={sort} onChange={event => { setSort(event.target.value); setPage(1); }}><option value="date_desc">日期从新到旧</option><option value="date_asc">日期从旧到新</option><option value="price_asc">价格从低到高</option><option value="price_desc">价格从高到低</option></select></label></div>
    <ErrorMessage message={records.error} />
    {records.loading ? <Loading /> : <div className="table-scroll"><table className="records-table"><caption className="sr-only">所选商品规格的报价明细</caption>
      <thead><tr><th>报价日期</th><th>区分组</th><th>监测市场</th><th className="numeric">原始价格</th><th>报价状态</th><th>质量提示</th><th>来源记录</th></tr></thead>
      <tbody>{records.data?.items.length ? records.data.items.map(item => <tr key={item.source_record_id}>
        <td className="date-cell">{item.quote_date}</td><td>{item.district}</td>
        <td><button className="market-link" onClick={() => chooseMarket(item.district, item.market_id)}>{item.market}</button></td>
        <td className="numeric price-cell">{item.price == null ? '—' : item.price_raw}<small>{unit}</small></td>
        <td><span className={`status-badge ${item.quote_status}`}>{item.quote_status === 'missing' ? '无报价' : '有报价'}</span></td>
        <td>{item.review_flags.length ? <span className="review-badge" title={item.review_flags.map(flag => flagNames[flag] ?? flag).join('、')}>待复核 · {item.review_flags.map(flag => flagNames[flag] ?? flag).join('、')}</span> : <span className="muted">—</span>}</td>
        <td><span className="source-record" title={`${item.source_filename}\n原始规格：${item.product_spec_raw}`}>#{item.source_row_number}</span></td>
      </tr>) : <tr><td colSpan={7} className="empty-table">没有符合当前明细筛选的记录</td></tr>}</tbody>
    </table></div>}
    <div className="pagination"><span>每页20条 · 共 {records.data?.total.toLocaleString() ?? '—'} 条记录</span><div>
      <button className="page-button" aria-label="上一页" disabled={page === 1 || records.loading} onClick={() => setPage(value => value - 1)}>上一页</button>
      <span>第 {page} / {pages ?? '—'} 页</span>
      <button className="page-button" aria-label="下一页" disabled={pages == null || page >= pages || records.loading} onClick={() => setPage(value => value + 1)}>下一页</button>
    </div></div>
  </section>;
}
