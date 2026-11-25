import { useEffect, useRef, useState } from 'react';
import { Icon } from './Icon';
import { ErrorMessage } from './ui';
import type { ImportJob, ImportLive, ImportPreview } from './importData';

const sourceDefault = 'https://cddrc.chengdu.gov.cn/cdsfzggw/scjg/pricelist.shtml';
const states: Record<string, string> = { queued: '等待处理', publishing: '发送报价中', streaming: '流处理接收中', building: '计算价格汇总', verifying: '核对数据', succeeded: '导入完成', no_op: '无需新增', failed: '处理失败' };
const classifications: Record<string, string> = { new: '新增', existing_duplicate: '已存在', file_duplicate: '文件内重复', conflict: '价格冲突', invalid: '无效记录' };
const finished = new Set(['succeeded', 'no_op', 'failed']);
const stamp = (value: string) => new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false });

async function responseJson<T>(response: Response): Promise<T> {
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail?.message ?? `操作未完成（${response.status}），请检查文件后重试`);
  return body as T;
}

export function DataManager({ live, refreshAnalysis }: { live: ImportLive; refreshAnalysis: () => void }) {
  const [file, setFile] = useState<File>();
  const [source, setSource] = useState(sourceDefault);
  const [sheet, setSheet] = useState('');
  const [preview, setPreview] = useState<ImportPreview>();
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string>();
  const [confirmed, setConfirmed] = useState<string>();
  const requestRef = useRef<AbortController | null>(null);
  useEffect(() => () => requestRef.current?.abort(), []);
  function resetPreview() {
    requestRef.current?.abort(); setBusy(false); setPreview(undefined); setConfirmed(undefined);
  }
  function chooseFile(next?: File) {
    requestRef.current?.abort(); setBusy(false); setPreview(undefined); setConfirmed(undefined); setError(undefined);
    if (next && (!/\.(csv|xlsx)$/i.test(next.name) || next.size > 20 * 1024 * 1024)) {
      setFile(undefined); setError('请选择不超过20MB的CSV或XLSX文件'); return;
    }
    setFile(next);
  }
  async function makePreview() {
    if (!file) return;
    requestRef.current?.abort();
    const controller = new AbortController(); requestRef.current = controller;
    setBusy(true); setError(undefined); setPreview(undefined); setConfirmed(undefined);
    const body = new FormData(); body.append('file', file); body.append('source_url', source);
    if (sheet.trim()) body.append('sheet_name', sheet.trim());
    try {
      const result = await responseJson<ImportPreview>(await fetch('/api/v1/imports/preview', { method: 'POST', body, signal: controller.signal }));
      if (!controller.signal.aborted) setPreview(result);
    } catch (problem) {
      if (!controller.signal.aborted) setError(problem instanceof Error ? problem.message : '文件预览失败');
    } finally { if (!controller.signal.aborted) setBusy(false); }
  }
  async function confirm() {
    if (!preview?.can_confirm) return;
    setConfirming(true); setError(undefined);
    try {
      const job = await responseJson<ImportJob>(await fetch('/api/v1/imports', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ preview_id: preview.preview_id }),
      }));
      live.acceptJob(job); setConfirmed(job.id); await live.refreshJobs();
    } catch (problem) { setError(problem instanceof Error ? problem.message : '确认导入失败'); }
    finally { setConfirming(false); }
  }
  return <main id="main-content" className="dashboard import-dashboard">
    <div className="page-heading"><div><div className="eyebrow">真实文件导入 · 历史数据保留</div><h1>数据管理</h1><p>先核对文件，再增量入库。相同报价跳过，冲突价格不会覆盖历史。</p></div>
      <span className={`service-badge ${live.service?.worker.online ? 'online' : ''}`}><Icon name="layers" size={18} />{live.service?.worker.online ? '持续流处理在线' : '流处理等待就绪'}</span>
    </div>
    {live.newRunId ? <div className="new-data-banner"><span>新一批报价已验收，图表可加载最新数据。</span><button className="button primary" onClick={refreshAnalysis}>加载新数据</button></div> : null}
    <section className="panel upload-panel">
      <div className="panel-heading"><div><h2>上传报价文件</h2><p>支持原始长表、宽表CSV，以及含报价工作表的Excel。每次最多100,000条报价。</p></div><span className="tag">CSV / XLSX · 20MB以内</span></div>
      <div className="upload-content">
        <label className="file-drop" onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); chooseFile(event.dataTransfer.files[0]); }}>
          <Icon name="upload" size={30} /><strong>{file ? file.name : '选择或拖入真实报价文件'}</strong>
          <span>{file ? `${(file.size / 1024).toFixed(1)} KB · ${confirmed ? '已登记，请查看下方任务状态' : '已选择，尚未入库'}` : '文件预览只校验内容，确认后才会创建导入任务'}</span>
          <input type="file" accept=".csv,.xlsx" aria-label="选择报价文件" onChange={event => chooseFile(event.target.files?.[0])} />
        </label>
        <div className="import-fields">
          <label>来源链接<input type="url" value={source} onChange={event => { setSource(event.target.value); resetPreview(); }} /></label>
          <label>Excel工作表（可选）<input type="text" value={sheet} placeholder="默认优先识别“长表”" onChange={event => { setSheet(event.target.value); resetPreview(); }} /></label>
        </div>
        <div className="import-actions"><button className="button primary" disabled={!file || busy || confirming} onClick={() => void makePreview()}><Icon name="filter" size={18} />{busy ? '正在读取并校验全部报价…' : '预览并校验'}</button><span>无报价请保留“-”；使用已登记商品规格和区市场组合。</span></div>
        <ErrorMessage message={error} />
      </div>
    </section>
    {preview ? <section className="panel preview-panel">
      <div className="panel-heading"><div><h2>导入预览</h2><p>{preview.filename} · {preview.sheet} · {preview.date_start ?? '日期无效'} 至 {preview.date_end ?? '日期无效'}</p></div><span className={`status-badge ${preview.can_confirm ? 'quoted' : 'missing'}`}>{preview.can_confirm ? '校验完成' : '需要修正'}</span></div>
      <div className="import-counts">{[
        ['读取记录', preview.source_rows], ['将新增', preview.counts.new], ['已存在', preview.counts.existing_duplicate],
        ['文件内重复', preview.counts.file_duplicate], ['价格冲突', preview.counts.conflict], ['无效记录', preview.counts.invalid],
      ].map(([label, value]) => <div key={label}><span>{label}</span><strong>{Number(value).toLocaleString()}</strong></div>)}</div>
      {preview.error_examples.length ? <div className="validation-errors" role="alert"><strong>请修正后重新上传，本次不会部分入库。</strong><ul>{preview.error_examples.map((row, index) => <li key={index}>第{row.input_row}行 · {row.product_name} · {row.market}：{row.errors.join('；')}</li>)}</ul></div> : null}
      <div className="table-scroll"><table className="preview-table"><caption className="sr-only">上传文件前20条预览</caption><thead><tr><th>源行号</th><th>报价日期</th><th>商品与原始规格</th><th>区与市场</th><th>原始报价</th><th>校验分类</th></tr></thead><tbody>{preview.sample.map((row, index) => <tr key={index}><td>{row.input_row}</td><td>{row.quote_date}</td><td>{row.product_name}<small>{row.product_spec}</small></td><td>{row.district}<small>{row.market}</small></td><td>{row.price_raw}<small>{row.unit}</small></td><td><span className={`import-class ${row.classification}`}>{classifications[row.classification]}</span></td></tr>)}</tbody></table></div>
      <div className="preview-footer"><div><strong>完整校验{preview.source_rows.toLocaleString()}条，预览显示前{preview.sample.length}条。</strong><p>{preview.counts.new ? `确认后将新增${preview.counts.new}条，并保留已有历史报价。` : '全部为重复记录时，不生成新的数据批次，只登记本次检查。'}</p></div><button className="button primary" disabled={!preview.can_confirm || confirming || Boolean(confirmed)} onClick={() => void confirm()}>{confirmed ? '已登记任务' : confirming ? '正在确认…' : '确认导入'}</button></div>
      <details className="upload-provenance"><summary>文件来源与核对标识</summary><p>来源：{preview.source_url}</p><p>文件 SHA-256：{preview.file_sha256}</p><p>预览参考批次：{preview.base_run_id}</p></details>
    </section> : null}
    <section className="panel jobs-panel"><div className="panel-heading"><div><h2>最近导入任务</h2><p>{live.connected ? '任务通知已连接' : '通知连接中，任务状态每5秒补充核对'} · 心跳{live.service?.worker.last_heartbeat ? ` ${stamp(live.service.worker.last_heartbeat)}` : '等待中'}</p></div><button className="button" onClick={() => void live.refreshJobs()}><Icon name="refresh" size={17} />刷新任务</button></div>
      {!live.jobs.length ? <div className="empty-state"><Icon name="upload" size={30} /><strong>还没有导入任务</strong><span>先上传文件并预览，确认后可以在这里查看实际进度。</span></div> : <div className="job-list">{live.jobs.map(job => <article className={`job-card ${job.id === confirmed ? 'recent' : ''}`} key={job.id}>
        <div className="job-heading"><div><strong>{job.filename}</strong><span>{stamp(job.created_at)}</span></div><span className={`job-state ${job.status}`}>{!finished.has(job.status) ? <span className="loader" /> : null}{states[job.status] ?? job.status}</span></div>
        <p>{job.message}</p><div className="job-counts"><span>文件记录 <strong>{job.source_rows.toLocaleString()}</strong></span><span>发送 <strong>{job.published_rows}/{job.expected_rows}</strong></span><span>接收 <strong>{job.received_rows}/{job.expected_rows}</strong></span><span>新增 <strong>{job.inserted_rows}</strong></span><span>跳过重复 <strong>{job.skipped_rows}</strong></span>{job.recovery_count ? <span>服务恢复 <strong>{job.recovery_count}次</strong></span> : null}</div>
        {job.expected_rows > 0 && ['publishing', 'streaming'].includes(job.status) ? <progress max={job.expected_rows} value={job.status === 'publishing' ? job.published_rows : job.received_rows} aria-label="已确认报价消息数量" /> : null}
        {job.error_message ? <ErrorMessage message={job.error_message} /> : null}
        {job.result_run_id ? <div className="job-result"><Icon name="check" size={16} />已生成并验收新数据批次；历史数据保留。</div> : null}
      </article>)}</div>}
    </section>
    <div className="import-help"><Icon name="info" size={18} /><p>此处处理真实报价文件。流式处理指新任务到达后的持续消费与校验，原始报价日期不会改成处理时间；发布价格仍保持源文件的周度频率。</p></div>
  </main>;
}
