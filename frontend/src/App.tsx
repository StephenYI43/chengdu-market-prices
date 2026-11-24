import { useState } from 'react';
import { endpoint, useResource } from './api';
import { Icon } from './Icon';
import { PricePanels } from './PricePanels';
import { PriceRecords } from './PriceRecords';
import { ErrorMessage, Loading, Shell } from './ui';
import type { Catalog, Market, Overview, Product, Selection } from './types';

export function App() {
  const [revision, setRevision] = useState(0);
  const overview = useResource<Overview>('/api/v1/overview', revision);
  const runId = overview.data?.meta.run_id;
  const products = useResource<Catalog<Product>>(runId ? endpoint('products', { run_id: runId }) : null, revision);
  const markets = useResource<Catalog<Market>>(runId ? endpoint('markets', { run_id: runId }) : null, revision);
  const error = overview.error ?? products.error ?? markets.error;
  const refresh = () => setRevision(value => value + 1);
  if (error) return <Shell><main id="main-content" className="initial-state"><Icon name="layers" size={36} /><h1>暂时无法读取数据</h1><ErrorMessage message={error} /><button className="button primary" onClick={refresh}><Icon name="refresh" />重新读取</button></main></Shell>;
  if (!overview.data || !products.data || !markets.data) return <Shell><main id="main-content" className="initial-state"><Loading text="正在连接已验收的价格数据" /></main></Shell>;
  if (products.data.meta.run_id !== runId || markets.data.meta.run_id !== runId) return <Shell><main id="main-content" className="initial-state"><ErrorMessage message="数据批次未对齐，请重新读取" /><button className="button primary" onClick={refresh}>重新读取</button></main></Shell>;
  return <Shell><Dashboard key={`${runId}:${revision}`} overview={overview.data} products={products.data.items} markets={markets.data.items} refresh={refresh} /></Shell>;
}

function Dashboard({ overview, products, markets, refresh }: { overview: Overview; products: Product[]; markets: Market[]; refresh: () => void }) {
  const [category, setCategory] = useState('');
  const [productId, setProductId] = useState(overview.default_product_id);
  const [district, setDistrict] = useState('');
  const [marketId, setMarketId] = useState('');
  const [dateFrom, setDateFrom] = useState(overview.quote_dates.at(-12) ?? overview.meta.date_start);
  const [dateTo, setDateTo] = useState(overview.meta.as_of);
  const [activeRange, setActiveRange] = useState(12);
  const product = products.find(item => item.product_id === productId) ?? products[0];
  const visibleMarkets = district ? markets.filter(item => item.district === district) : markets;
  const rangeError = !dateFrom || !dateTo ? '请选择完整的日期区间' : dateFrom > dateTo ? '开始日期不能晚于结束日期' : undefined;
  const comparisonDate = overview.quote_dates.filter(day => day <= dateTo && day >= dateFrom).at(-1) ?? dateTo;
  const selection: Selection = { run_id: overview.meta.run_id, product_id: product.product_id, district, market_id: marketId, date_from: dateFrom, date_to: dateTo };
  const chartDates = overview.quote_dates.filter(day => day >= dateFrom && day <= dateTo);
  const scope = marketId ? visibleMarkets.find(item => item.market_id === marketId)?.market : district ? `${district}监测市场` : '全部监测市场';
  function chooseProduct(id: string) {
    const next = products.find(item => item.product_id === id);
    if (!next) return;
    setProductId(id);
    if (dateTo > next.last_observed_week || dateTo < next.first_observed_week) {
      setDateTo(next.last_observed_week);
      if (dateFrom > next.last_observed_week || dateTo < next.first_observed_week) setDateFrom(next.first_observed_week);
      setActiveRange(-1);
    }
  }
  function chooseRange(weeks: number) {
    setDateFrom(weeks ? overview.quote_dates.at(-weeks) ?? overview.meta.date_start : overview.meta.date_start);
    setDateTo(overview.meta.as_of); setActiveRange(weeks);
  }
  return <>
    <header className="topbar"><span className="breadcrumb">城市生活数据<span>/</span><strong>价格观察</strong></span><div className="topbar-meta"><span className="verified"><Icon name="check" size={15} />数据已核对</span><a href="/docs" target="_blank" rel="noreferrer">接口文档</a></div></header>
    <main id="main-content" className="dashboard">
      <div className="page-heading"><div><div className="eyebrow">成都市农贸市场 · 周度监测</div><h1>农贸市场价格观察</h1><p>查看同一规格的周度价格，比较不同市场的有效报价。</p></div><div className="heading-actions"><span className="as-of"><Icon name="calendar" size={17} />截至 {overview.meta.as_of}</span><button className="button" onClick={refresh}><Icon name="refresh" size={17} />刷新数据</button></div></div>
      <div className="dataset-strip"><span><Icon name="layers" size={17} /><strong>{overview.totals.source_rows.toLocaleString()}</strong>条原始记录</span><span><strong>{overview.totals.markets}</strong>个监测市场</span><span><strong>{overview.totals.periods}</strong>期周度数据</span><span>{overview.meta.date_start} 至 {overview.meta.as_of}</span><a href="#data-notes">查看统计口径</a></div>
      <section className="filter-panel" aria-label="价格查询筛选">
        <div className="filter-heading"><Icon name="filter" size={18} /><strong>选择观察范围</strong><span>按同商品规格比较</span></div>
        <div className="filter-grid">
          <label className="field category-field">商品分类<select value={category} onChange={event => { const value = event.target.value; setCategory(value); const first = products.find(item => item.category === value); if (value && product.category !== value && first) chooseProduct(first.product_id); }}><option value="">全部分类</option>{overview.categories.map(item => <option key={item}>{item}</option>)}</select></label>
          <label className="field product-field">商品 / 规格<select value={product.product_id} onChange={event => chooseProduct(event.target.value)}>{products.filter(item => !category || item.category === category).map(item => <option key={item.product_id} value={item.product_id}>{item.product_name} · {item.product_spec}{item.current ? '' : '（历史规格）'}</option>)}</select></label>
          <label className="field">监测区分组<select value={district} onChange={event => { setDistrict(event.target.value); setMarketId(''); }}><option value="">全部区分组</option>{overview.districts.map(item => <option key={item}>{item}</option>)}</select></label>
          <label className="field market-field">监测市场<select value={marketId} onChange={event => setMarketId(event.target.value)}><option value="">全部市场</option>{visibleMarkets.map(item => <option key={item.market_id} value={item.market_id}>{item.market}</option>)}</select></label>
        </div>
        <div className="date-row"><div className="date-inputs"><label>开始日期<input aria-label="开始日期" type="date" value={dateFrom} onInput={event => { setDateFrom(event.currentTarget.value); setActiveRange(-1); }} onChange={event => { setDateFrom(event.target.value); setActiveRange(-1); }} /></label><span className="date-separator">至</span><label>结束日期<input aria-label="结束日期" type="date" value={dateTo} onInput={event => { setDateTo(event.currentTarget.value); setActiveRange(-1); }} onChange={event => { setDateTo(event.target.value); setActiveRange(-1); }} /></label></div><div className="range-buttons" aria-label="快捷时间区间">{([{ weeks: 12, label: '近12周' }, { weeks: 26, label: '近半年' }, { weeks: 0, label: '全部历史' }]).map(({ weeks, label }) => <button key={weeks} aria-pressed={activeRange === weeks} className={activeRange === weeks ? 'active' : ''} onClick={() => chooseRange(weeks)}>{label}</button>)}</div><span className="scope-note">计价单位：<strong>{product.unit}</strong></span></div>
        <ErrorMessage message={rangeError} />
      </section>
      <div className="section-context"><div><strong>{product.product_name}</strong><span>{product.product_spec}</span><span className="scope-pill">{scope}</span></div><span>比较日期 {comparisonDate}</span></div>
      <PricePanels selection={selection} comparisonDate={comparisonDate} dates={chartDates} rangeError={rangeError} unit={product.unit} />
      <PriceRecords key={JSON.stringify(selection)} selection={selection} rangeError={rangeError} unit={product.unit} chooseMarket={(area, id) => { setDistrict(area); setMarketId(id); }} />
      <footer className="data-notes" id="data-notes"><details><summary><Icon name="info" size={18} />关于这些价格数据</summary><p>数据来自用户采集的<a href={overview.meta.source_url} target="_blank" rel="noreferrer">成都市发改委市场价格栏目</a>文件，尚未逐期对照官网。均价是所选监测市场的有效价格等权均值，不能代表按全市消费量加权的价格指数。</p><p>“无报价”表示源记录为“-”，“无记录”表示该期未出现该商品规格。质量提示仅用于复核，原价保留。周变化以连续两期都有价格的相同市场计算。</p><div className="provenance"><span>来源文件：{overview.meta.source_filename}</span><span>数据批次：{overview.meta.run_id}</span><span>文件 SHA-256：{overview.meta.source_file_sha256}</span></div></details><span className="footer-caption">真实周度报价 · 同规格比较 · 缺失值保留</span></footer>
    </main>
  </>;
}
