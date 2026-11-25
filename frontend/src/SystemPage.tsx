import { useEffect, useState } from 'react';
import { useResource } from './api';
import { Icon } from './Icon';
import { ErrorMessage, Loading } from './ui';

interface Status {
  checked_at: string; kafka: { online: boolean; brokers: number };
  stream: { online: boolean; last_heartbeat: string | null };
  forecast_service: { online: boolean; status?: string; message?: string; error?: string };
  dataset: { meta: { run_id: string; as_of: string }; totals: { source_rows: number; periods: number } } | null;
  current_model: { model_run_id: string; as_of: string; ready_products: number } | null;
  source_cadence: { age_days: number; next_expected_week: string; may_need_update: boolean } | null;
  recent_job_counts: Record<string, number>; issues: string[];
}
const stamp = (s: string) => new Date(s).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false });

export function SystemPage() {
  const [revision, setRevision] = useState(0);
  const resource = useResource<Status>('/api/v1/system/status', revision);
  useEffect(() => { const timer = window.setInterval(() => setRevision(v => v+1), 20000); return () => window.clearInterval(timer); }, []);
  const data = resource.data;
  return <main className="dashboard system-dashboard" id="main-content"><div className="page-heading"><div><div className="eyebrow">实际服务状态 · 业务日期与处理时间分开</div><h1>系统运行概况</h1><p>核对采集文件所在批次、持续消费者和预测服务，每20秒刷新。</p></div><button className="button" onClick={() => setRevision(v => v+1)}><Icon name="refresh" size={17} />立即检查</button></div><ErrorMessage message={resource.error} />{resource.loading ? <Loading text="正在核对本机服务状态" /> : data ? <>
    <p className="system-checked">本次检查 {stamp(data.checked_at)}（北京时间）</p><section className="system-services">{[['Kafka消息服务', data.kafka.online, `${data.kafka.brokers}个可连接Broker`], ['持续流处理', data.stream.online, data.stream.last_heartbeat ? `最近心跳 ${stamp(data.stream.last_heartbeat)}` : '尚未收到心跳'], ['预测训练服务', data.forecast_service.online, data.forecast_service.message ?? '等待任务'], ['当前批次预测', Boolean(data.current_model), data.current_model ? `${data.current_model.ready_products}个规格 · 截至${data.current_model.as_of}` : '等待匹配当前批次的模型']].map(([label, online, note]) => <article className="panel service-card" key={String(label)}><div><h2>{label}</h2><span className={`review-state ${online ? 'reviewed' : ''}`}>{online ? '可用' : '等待检查'}</span></div><p>{note}</p></article>)}</section>
    {data.issues.map((issue, i) => <ErrorMessage key={i} message={issue} />)}<ErrorMessage message={data.forecast_service.error} />
    <section className="panel system-dataset"><div className="panel-heading"><div><h2>当前数据与周度更新</h2><p>网页处理时间和心跳持续变化，真实报价仍按来源的周度日期显示。</p></div></div>{data.dataset ? <div className="system-dataset-body"><strong>{data.dataset.totals.source_rows.toLocaleString()}条真实来源记录 · {data.dataset.totals.periods}期</strong><p>报价周截至 {data.dataset.meta.as_of} · 距检查日期 {data.source_cadence?.age_days}天</p><p>下一期参考日期 {data.source_cadence?.next_expected_week}，仅按7天频率推算，未据此判定官网已经发布。</p>{data.source_cadence?.may_need_update ? <p className="cadence-note">已超过下一期参考日期，可核对是否需要补充真实文件。</p> : null}<p>数据批次：{data.dataset.meta.run_id}</p><p>模型批次：{data.current_model?.model_run_id ?? '尚未就绪'}</p></div> : <div className="system-dataset-body">已验收数据暂不可用。</div>}</section>
    <section className="panel system-jobs"><div className="panel-heading"><div><h2>最近20个导入任务</h2><p>以下为实际已登记任务的状态计数，可到数据管理查看详情。</p></div><a className="button" href="#data-management">查看导入任务</a></div><div className="system-job-counts">{Object.entries(data.recent_job_counts).length ? Object.entries(data.recent_job_counts).map(([state, count]) => <span key={state}>{({ no_op: '无需新增', succeeded: '已完成', failed: '失败', queued: '等待', publishing: '发送中', streaming: '接收中', building: '计算中', verifying: '核对中' } as Record<string, string>)[state] ?? state}<strong>{count}</strong></span>) : <span>尚无导入任务</span>}</div></section>
  </> : null}</main>;
}
