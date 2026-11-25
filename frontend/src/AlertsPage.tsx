import { useEffect, useRef, useState } from 'react';
import { endpoint, money, percent, useResource } from './api';
import { Icon } from './Icon';
import { ErrorMessage, Loading } from './ui';
import type { DatasetMeta, Market, Product } from './types';

interface Review { reviewed: boolean; note: string; version: number; updated_at: string | null }
interface AlertEvent {
  id: string; rule: string; rule_name: string; scope: string; quote_date: string;
  product_id: string; product_name: string; product_spec: string; unit: string;
  district: string | null; market: string | null; market_id: string | null;
  price_raw: string | null; product_spec_raw: string | null; source_filename: string | null;
  source_row_number: number | null; source_record_id: string | null; source_file_sha256: string | null;
  source_url: string | null; evidence: Record<string, string | number | null>; review: Review;
}
interface Rules { version: string; policy_sha256: string; weekly_change_ratio: number; peer_high_ratio: number; peer_low_ratio: number; minimum_market_coverage: number; names: Record<string, string> }
interface AlertList { meta: DatasetMeta; rules: Rules; total: number; page: number; page_size: number;
  summary: { matched_events: number; open_events: number; reviewed_events: number; affected_quotes: number; rule_counts: Record<string, number> }; items: AlertEvent[] }
interface AlertDetail { meta: DatasetMeta; rules: Rules; event: AlertEvent; history: { seq: number; reviewed: boolean; note: string; created_at: string; run_id: string }[] }

const stamp = (s: string) => new Date(s).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false });
const value = (v: string | number | null | undefined) => money(v == null ? null : String(v));
function explain(event: AlertEvent) {
  const e = event.evidence;
  if (event.rule === 'large_weekly_change') return `${value(e.previous_price)} → ${value(e.price)} · ${percent(Number(e.change_ratio))}`;
  if (['high_vs_peer_median', 'low_vs_peer_median'].includes(event.rule)) return `报价 ${value(e.price)} · 中位数 ${value(e.peer_median)} · ${Number(e.peer_ratio).toFixed(2)}倍`;
  if (event.rule === 'source_no_quote') return '源文件为“-”，保留无报价状态';
  if (event.rule === 'market_not_observed') return '该规格当期有报价，此市场未找到记录';
  return `有效市场 ${e.quoted_markets}/${e.expected_markets}，最低要求 ${Math.ceil(Number(e.expected_markets)*Number(e.minimum_ratio))}`;
}

export function AlertsPage({ meta, products, markets, dates }: { meta: DatasetMeta; products: Product[]; markets: Market[]; dates: string[] }) {
  const [filter, setFilter] = useState({ date_from: meta.as_of, date_to: meta.as_of, product_id: '', district: '', market_id: '', rule: '', status: '' });
  const [page, setPage] = useState(1);
  const [revision, setRevision] = useState(0);
  const [selected, setSelected] = useState<AlertEvent>();
  function change(values: Partial<typeof filter>) { setFilter(previous => ({ ...previous, ...values })); setPage(1); }
  const rangeError = !filter.date_from || !filter.date_to ? '请选择完整日期区间' : filter.date_from > filter.date_to ? '开始日期不能晚于结束日期' : undefined;
  const params = { run_id: meta.run_id, ...filter };
  const list = useResource<AlertList>(rangeError ? null : endpoint('alerts', { ...params, page }), revision);
  const data = list.data;
  const rules = data?.rules;
  const areas = [...new Set(markets.map(m => m.district))].sort();
  return <main id="main-content" className="dashboard alerts-dashboard">
    <div className="page-heading"><div><div className="eyebrow">真实报价核对 · 保留处理记录</div><h1>价格预警与复核</h1><p>查看规则命中的价格变化和数据缺口，追溯来源并记录复核结果。</p></div><button className="button" onClick={() => setRevision(v => v+1)}><Icon name="refresh" size={17} />刷新预警</button></div>
    <div className="alert-explanation"><Icon name="info" size={19} /><p>规则命中表示需要留意或核对。标记“已复核”仅记录检查状态，原始报价继续保留；同一报价可能命中多项规则。</p></div>
    <section className="filter-panel alert-filters" aria-label="预警筛选">
      <div className="alert-filter-grid"><label className="field">商品与规格<select value={filter.product_id} onChange={e => change({ product_id: e.target.value })}><option value="">全部商品规格</option>{products.map(p => <option key={p.product_id} value={p.product_id}>{p.product_name} · {p.product_spec}</option>)}</select></label><label className="field">区分组<select value={filter.district} onChange={e => change({ district: e.target.value, market_id: '' })}><option value="">全部区分组</option>{areas.map(area => <option key={area}>{area}</option>)}</select></label><label className="field">监测市场<select value={filter.market_id} onChange={e => change({ market_id: e.target.value })}><option value="">全部市场</option>{markets.filter(m => !filter.district || m.district === filter.district).map(m => <option key={m.market_id} value={m.market_id}>{m.market}</option>)}</select></label></div>
      <div className="alert-filter-grid secondary"><label className="field">提示类型<select value={filter.rule} onChange={e => change({ rule: e.target.value })}><option value="">全部类型</option>{Object.entries(rules?.names ?? {}).map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></label><label className="field">复核状态<select value={filter.status} onChange={e => change({ status: e.target.value })}><option value="">全部状态</option><option value="open">待复核</option><option value="reviewed">已复核</option></select></label><div className="alert-window-actions"><button className="button" onClick={() => change({ date_from: meta.as_of, date_to: meta.as_of })}>最新一期</button><button className="button" onClick={() => change({ date_from: dates.at(-12) ?? meta.date_start, date_to: meta.as_of })}>近12周</button><button className="button" onClick={() => change({ date_from: meta.date_start, date_to: meta.as_of })}>全部历史</button></div></div>
      <div className="date-row"><div className="date-inputs"><label>开始日期<input aria-label="预警开始日期" type="date" value={filter.date_from} onInput={e => change({ date_from: e.currentTarget.value })} onChange={e => change({ date_from: e.target.value })} /></label><span>至</span><label>结束日期<input aria-label="预警结束日期" type="date" value={filter.date_to} onInput={e => change({ date_to: e.currentTarget.value })} onChange={e => change({ date_to: e.target.value })} /></label></div><span className="scope-note">报价周截至 {meta.as_of}</span></div><ErrorMessage message={rangeError} />
    </section>
    <ErrorMessage message={list.error} />
    <section className="metric-grid alert-counts" aria-label="当前范围预警概况" aria-busy={list.loading}>{[['规则命中', data?.summary.matched_events], ['待复核', data?.summary.open_events], ['已复核', data?.summary.reviewed_events], ['涉及来源报价', data?.summary.affected_quotes]].map(([label, count]) => <article className="metric-card" key={label}><div className="metric-label">{label}</div><div className="metric-value">{count == null ? '—' : Number(count).toLocaleString()}</div><p>{label === '涉及来源报价' ? '按来源记录去重，覆盖提示另计' : '按当前日期、商品、市场和类型统计'}</p></article>)}</section>
    <section className="panel alert-list"><div className="panel-heading"><div><h2>预警记录</h2><p>每一项均可查看触发依据、原始规格与复核历史。</p></div><a className={`button ${rangeError ? 'disabled' : ''}`} href={endpoint('alerts/export.csv', params)}><Icon name="download" size={17} />导出筛选结果</a></div>
      {list.loading ? <Loading text="正在核对真实报价与规则" /> : data?.items.length ? <div className="table-scroll"><table><caption className="sr-only">预警与复核记录</caption><thead><tr><th>报价日期</th><th>商品与规格</th><th>监测范围</th><th>提示与依据</th><th>复核状态</th><th>操作</th></tr></thead><tbody>{data.items.map(event => <tr key={event.id}><td>{event.quote_date}</td><td>{event.product_name}<small>{event.product_spec} · {event.unit}</small></td><td>{event.market ?? '全市监测市场'}<small>{event.district ?? '市级覆盖'}</small></td><td><span className={`alert-rule ${event.rule === 'source_no_quote' ? 'missing' : ''}`}>{event.rule_name}</span><small>{explain(event)}</small></td><td><span className={`review-state ${event.review.reviewed ? 'reviewed' : ''}`}>{event.review.reviewed ? '已复核' : '待复核'}</span></td><td><button className="button" onClick={() => setSelected(event)}>查看与复核</button></td></tr>)}</tbody></table></div> : !list.error && !rangeError ? <div className="empty-state"><Icon name="check" size={28} /><strong>当前筛选下没有预警</strong><span>可以调整日期、提示类型或复核状态继续查看。</span></div> : null}
      {data ? <div className="pagination"><span>筛选结果 {data.total} 项 · 每页20项</span><div><button className="button" disabled={page <= 1} onClick={() => setPage(p => p-1)}>上一页</button><span>第{page}/{Math.max(1, Math.ceil(data.total/20))}页</span><button className="button" disabled={page*20 >= data.total} onClick={() => setPage(p => p+1)}>下一页</button></div></div> : null}
    </section>
    {rules ? <details className="alert-rules"><summary>查看规则与统计口径</summary><ul><li>连续7天的同市场、同规格价格涨跌幅绝对值达到{rules.weekly_change_ratio*100}%。</li><li>价格达到同周同规格市场中位数的{rules.peer_high_ratio}倍，或不超过{rules.peer_low_ratio}倍。</li><li>源文件为“-”单独提示；当期已出现的规格缺少某市场记录时提示缺记录。</li><li>有效报价市场少于目标市场数的{rules.minimum_market_coverage*100}%时提示覆盖不足。整期未出现的季节性或停报规格不据此补造缺报价。</li><li>市级覆盖提示不属于某一个区或市场；筛选区、市场时仅显示对应市场事件。复核状态筛选影响列表，概况同时保留待复核和已复核数。</li></ul><p>规则版本 {rules.version} · 数据批次 {meta.run_id}</p></details> : null}
    {selected ? <ReviewDialog runId={meta.run_id} event={selected} close={() => setSelected(undefined)} changed={() => setRevision(v => v+1)} /> : null}
  </main>;
}

function ReviewDialog({ runId, event, close, changed }: { runId: string; event: AlertEvent; close: () => void; changed: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const [revision, setRevision] = useState(0);
  const detail = useResource<AlertDetail>(endpoint(`alerts/${event.id}`, { run_id: runId, quote_date: event.quote_date }), revision);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  useEffect(() => { const dialog = ref.current; dialog?.showModal(); return () => dialog?.close(); }, []);
  useEffect(() => { if (detail.data) setNote(detail.data.event.review.note); }, [detail.data]);
  async function save(reviewed: boolean) {
    if (!detail.data) return;
    setBusy(true); setError(undefined);
    try {
      const response = await fetch(`/api/v1/alerts/${event.id}/review`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ run_id: runId, quote_date: event.quote_date, policy_sha256: detail.data.rules.policy_sha256, expected_version: detail.data.event.review.version, reviewed, note }) });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail?.message ?? '复核记录保存失败');
      setRevision(v => v+1); changed();
    } catch (problem) { setError(problem instanceof Error ? problem.message : '保存失败'); }
    finally { setBusy(false); }
  }
  const current = detail.data?.event;
  return <dialog className="review-dialog" ref={ref} aria-labelledby="review-title" onCancel={e => { e.preventDefault(); if (!busy) close(); }}><div className="review-dialog-heading"><h2 id="review-title">预警依据与复核</h2><button className="button" disabled={busy} onClick={close}>关闭详情</button></div><ErrorMessage message={detail.error ?? error} />{detail.loading ? <Loading text="正在读取来源与复核记录" /> : current ? <>
    <div className="review-subject"><strong>{current.product_name} · {current.product_spec}</strong><span>{current.quote_date} · {current.market ?? '全市监测市场'} · {current.unit}</span><span className="alert-rule">{current.rule_name}</span><p>{explain(current)}</p>{current.evidence.previous_quote_date ? <p>前期报价日：{current.evidence.previous_quote_date}</p> : null}</div>
    <dl className="review-evidence">{Object.entries(current.evidence).map(([key, val]) => <div key={key}><dt>{({ price: '本期价格', previous_price: '前期价格', previous_quote_date: '前期报价日', change_ratio: '涨跌幅比率', threshold_ratio: '规则阈值比率', peer_median: '同周市场中位数', peer_ratio: '与中位数比率', source_marker: '原始无报价符号', quoted_markets: '有效报价市场数', observed_markets: '观察到的市场数', expected_markets: '目标市场数', minimum_ratio: '最低覆盖比率', reason: '缺记录说明' } as Record<string, string>)[key] ?? key}</dt><dd>{val == null ? '无报价' : String(val)}</dd></div>)}</dl>
    <details className="review-source"><summary>来源记录与核对标识</summary><p>来源文件：{current.source_filename ?? '覆盖/缺记录提示由真实市场矩阵计算，无单条源报价'}</p>{current.source_row_number ? <p>CSV记录号：{current.source_row_number} · 原始规格：{current.product_spec_raw} · 原始价格：{current.price_raw}</p> : null}{current.source_url ? <a href={current.source_url} target="_blank" rel="noreferrer">查看来源栏目</a> : null}<p>来源记录ID：{current.source_record_id ?? '不适用'}</p><p>来源SHA-256：{current.source_file_sha256 ?? '见数据批次来源清单'}</p><p>数据批次：{runId}</p></details>
    <div className="review-form"><div className="review-form-heading"><strong>复核记录</strong><span className={`review-state ${current.review.reviewed ? 'reviewed' : ''}`}>{current.review.reviewed ? '已复核' : '待复核'}</span></div><label>复核备注<textarea maxLength={500} rows={3} value={note} onChange={e => setNote(e.target.value)} placeholder="可记录核对结果或后续处理说明" /></label><small>{note.length}/500字 · 状态和备注单独保存，原报价保留。</small><div className="review-actions"><button className="button primary" disabled={busy} onClick={() => void save(true)}>{busy ? '正在保存…' : current.review.reviewed ? '保存复核备注' : '标记已复核'}</button>{current.review.reviewed ? <button className="button" disabled={busy} onClick={() => void save(false)}>撤销复核</button> : null}</div></div>
    <details className="review-history" open><summary>操作历史（最近50次）</summary>{detail.data?.history.length ? <ol>{detail.data.history.map(item => <li key={item.seq}><strong>{item.reviewed ? '标记已复核' : '撤销复核'}</strong><span>{stamp(item.created_at)}</span><p>{item.note || '未填写备注'}</p></li>)}</ol> : <p>尚无复核操作。</p>}</details>
  </> : null}</dialog>;
}
