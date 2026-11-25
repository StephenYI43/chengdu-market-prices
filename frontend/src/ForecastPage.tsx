import { lazy, Suspense, useEffect, useState } from 'react';
import { endpoint, money, useResource } from './api';
import { Icon } from './Icon';
import { ErrorMessage, Loading } from './ui';
import type { ForecastCatalog, ForecastDetail, Score } from './forecastTypes';

const ForecastChart = lazy(() => import('./Charts').then(module => ({ default: module.ForecastChart })));
const reasons: Record<string, string> = { short_history: '覆盖充足的历史不足52周', low_history_coverage: '历史覆盖率不足90%', recent_gaps: '最近未连续26周达到覆盖要求', no_adequate_current_week: '最新一期没有充足报价' };
const rate = (value: number | null | undefined) => value == null ? '—' : `${(100 * value).toFixed(2)}%`;

export function ForecastPage({ runId, defaultProduct }: { runId: string; defaultProduct: string }) {
  const [revision, setRevision] = useState(0);
  const [productId, setProductId] = useState(defaultProduct);
  const catalog = useResource<ForecastCatalog>(endpoint('forecasts/catalog', { run_id: runId }), revision);
  useEffect(() => {
    if (catalog.data?.status === 'ready') return;
    const timer = window.setInterval(() => setRevision(value => value + 1), 5000);
    return () => window.clearInterval(timer);
  }, [catalog.data?.status]);
  const ready = catalog.data?.status === 'ready';
  const detail = useResource<ForecastDetail>(ready ? endpoint('forecasts', { run_id: runId, product_id: productId, model_run_id: catalog.data?.report?.model_run_id }) : null, revision);
  const result = detail.data;
  const report = catalog.data?.report;
  const csv = endpoint('forecasts/export.csv', { run_id: runId, product_id: productId, model_run_id: report?.model_run_id });
  return <main id="main-content" className="dashboard forecast-dashboard">
    <div className="page-heading"><div><div className="eyebrow">真实历史训练 · 按周滚动回测</div><h1>周度价格预测</h1><p>预测未来1—4周的监测市场均价，结合独立回测误差判断参考价值。</p></div><button className="button" onClick={() => setRevision(value => value + 1)}><Icon name="refresh" size={17} />刷新预测</button></div>
    <div className="forecast-disclaimer"><Icon name="info" size={20} /><p>以下为模型计算结果。历史监测报价保留原值；预测值与误差参考带只用于分析，未来实际价格可能超出范围。</p></div>
    <ErrorMessage message={catalog.error ?? detail.error} />
    {!ready ? <section className="panel forecast-waiting"><Loading text={catalog.data?.service.status === 'failed' ? '该批次的预测尚未生成' : '正在等待该数据批次的预测与回测'} /><p>{catalog.data?.service.message ?? '首次训练需要完成多个历史时点的拟合与核对。'} · {catalog.data?.service.online ? '训练服务在线' : '训练服务尚未就绪'}</p><ErrorMessage message={catalog.data?.service.error} /></section> : <>
      <div className="forecast-context"><span><Icon name="calendar" size={17} />训练截至 <strong>{report?.as_of}</strong></span><span><strong>{report?.ready_products}</strong> 个规格可预测</span><span><strong>{report?.excluded_products}</strong> 个规格暂不预测</span><span>市级有效市场等权均价</span></div>
      <section className="filter-panel forecast-filter"><label className="field">选择商品与规格<select value={productId} onChange={event => setProductId(event.target.value)}>{catalog.data?.items.map(item => <option key={item.product.product_id} value={item.product.product_id}>{item.product.product_name} · {item.product.product_spec}{item.status !== 'ready' ? '（暂不预测）' : ''}</option>)}</select></label><div><span>预测范围</span><strong>未来4周 · 每周二</strong></div><a className="button" href={csv}><Icon name="download" size={17} />导出预测CSV</a></section>
      {detail.loading ? <Loading text="正在读取预测与回测结果" /> : result ? <>
        {result.status !== 'ready' ? <section className="panel exclusion-panel"><Icon name="info" size={26} /><h2>这个规格暂不生成预测</h2><ul>{result.profile?.exclusion_reasons.map(reason => <li key={reason}>{reasons[reason] ?? reason}</li>)}</ul><p>覆盖充足历史{result.profile?.adequate_weeks ?? 0}周 · 最近连续{result.profile?.recent_consecutive_adequate_weeks ?? 0}周 · 历史覆盖{rate(result.profile?.history_coverage)}</p><p>积累足够真实报价后，训练服务会重新判断。</p></section> : <>
          <div className="section-context"><div><strong>{result.product.product_name}</strong><span>{result.product.product_spec}</span><span className="scope-pill">{report?.model_names[result.selected_model ?? '']}</span></div><span>计价单位：{result.product.unit}</span></div>
          <section className="metric-grid forecast-cards" aria-label="未来四周预测均价">{result.forecasts.map(point => <article className="metric-card" key={point.horizon}><div className="metric-label">第{point.horizon}周 · {point.target_date.slice(5).replace('-', '/')}<span className="prediction-tag">预测</span></div><div className="metric-value"><small>¥</small>{money(point.predicted)}</div><p>误差参考带 {point.lower == null ? '样本不足' : `${money(point.lower)}—${money(point.upper)}`}</p></article>)}</section>
          <section className="panel forecast-chart-panel"><div className="panel-heading"><div><h2>历史走势与未来预测</h2><p>实线为真实历史均价，虚线为预测；浅色区域为历史误差参考带。</p></div><span className="tag">{result.product.unit}</span></div><Suspense fallback={<Loading text="正在加载预测图表" />}><ForecastChart result={result} /></Suspense><p className="panel-note">参考带使用验证期同预测跨度的绝对误差80%分位，每个跨度{result.forecasts[0]?.reference_samples ?? 0}次历史预测；它不保证未来覆盖率。</p><details className="data-details"><summary>查看预测数值</summary><div className="table-scroll"><table><thead><tr><th>目标日期</th><th>跨度</th><th>预测均价</th><th>参考带下限</th><th>参考带上限</th></tr></thead><tbody>{result.forecasts.map(point => <tr key={point.horizon}><td>{point.target_date}</td><td>{point.horizon}周</td><td>{money(point.predicted)}</td><td>{money(point.lower)}</td><td>{money(point.upper)}</td></tr>)}</tbody></table></div></details></section>
          <section className="panel forecast-evaluation"><div className="panel-heading"><div><h2>独立时段回测</h2><p>模型在每个历史截止期重新训练。下面的真实结果没有参与模型选择。</p></div><span className="tag">{report?.test_origins.length}个滚动截止期 × 4周</span></div><div className="forecast-test-strip"><span>截止期 {report?.test_origins[0]} 至 {report?.test_origins.at(-1)}</span><span>误差越低越好 · 单位 {result.product.unit}</span></div><div className="table-scroll"><table><caption className="sr-only">三种方法的独立回测误差</caption><thead><tr><th>预测方法</th><th>MAE</th><th>RMSE</th><th>MAPE</th><th>WAPE</th><th>有效预测次数</th></tr></thead><tbody>{Object.entries(result.evaluation?.models ?? {}).map(([model, score]) => <tr key={model}><td>{report?.model_names[model]} {model === result.selected_model ? <span className="import-class new">验证期选用</span> : null}</td><ScoreCells score={score} /></tr>)}</tbody></table></div><p className="panel-note">复杂模型可能弱于简单基线。选用方法依据更早验证期的MAE确定；结果接近时优先沿用上周价，不根据上表事后挑选。</p><details className="data-details"><summary>查看选模依据与不同跨度误差</summary><p className="panel-note">验证截止期 {report?.validation_origins[0]} 至 {report?.validation_origins.at(-1)}；全部验证实际值最晚在{report?.selection_available_at}已知。</p><div className="table-scroll"><table><thead><tr><th>验证方法</th><th>验证MAE</th><th>验证RMSE</th><th>验证MAPE</th><th>验证WAPE</th><th>次数</th></tr></thead><tbody>{Object.entries(result.validation_scores ?? {}).map(([model, score]) => <tr key={model}><td>{report?.model_names[model]}</td><ScoreCells score={score} /></tr>)}</tbody></table><table><thead><tr><th>选用方法跨度</th><th>回测MAE</th><th>回测RMSE</th><th>回测MAPE</th><th>回测WAPE</th><th>次数</th></tr></thead><tbody>{Object.entries(result.evaluation?.selected_by_horizon ?? {}).map(([horizon, score]) => <tr key={horizon}><td>未来{horizon}周</td><ScoreCells score={score} /></tr>)}</tbody></table></div></details><details className="data-details"><summary>查看真实回测明细（{result.evaluation?.rows.length ?? 0}次）</summary><div className="table-scroll"><table><thead><tr><th>训练截止期</th><th>目标报价期</th><th>跨度</th><th>当时预测</th><th>实际均价</th><th>绝对误差</th></tr></thead><tbody>{result.evaluation?.rows.map(row => <tr key={`${row.origin}-${row.horizon}`}><td>{row.origin}</td><td>{row.target_date}</td><td>{row.horizon}周</td><td>{money(String(row.predicted))}</td><td>{money(String(row.actual))}</td><td>{money(String(Math.abs(row.predicted-row.actual)))}</td></tr>)}</tbody></table></div></details></section>
        </>}
        <details className="forecast-provenance"><summary>模型版本、数据来源与训练口径</summary><p>数据批次：{runId}</p><p>模型批次：{result.model_run_id}</p><p>LightGBM {report?.lightgbm_version} · MLflow实验 {report?.mlflow_run_id}</p><p>不足15/18个有效报价市场的周不用于训练或评分。使用历史滞后价、均值、波动和已知周历；不使用未来真实报价、不填补缺失价格。本次历史回测假定该周报价在截止期已可见，源文件未提供逐条发布时间与历史修订版本。</p></details>
      </> : null}
    </>}
  </main>;
}

function ScoreCells({ score }: { score: Score | null }) {
  return <><td>{money(score ? String(score.mae) : null)}</td><td>{money(score ? String(score.rmse) : null)}</td><td>{rate(score?.mape)}</td><td>{rate(score?.wape)}</td><td>{score?.n ?? '—'}</td></>;
}
