import type { ReactNode } from 'react';
import { Icon } from './Icon';

export function Shell({ children }: { children: ReactNode }) {
  return <div className="app-shell">
    <a className="skip-link" href="#main-content">跳到主要内容</a>
    <aside className="sidebar">
      <a className="brand" href="#main-content"><span className="brand-mark"><Icon name="leaf" size={26} /></span><span>蓉城价览<small>CHENGDU MARKETS</small></span></a>
      <div className="nav-caption">价格观察工作台</div>
      <nav aria-label="主要导航">
        <a className={!['#data-management', '#forecasts'].includes(window.location.hash) ? 'nav-overview' : ''} href="#main-content"><Icon name="grid" />价格总览<span>周度</span></a>
        <a href="#price-trends"><Icon name="chart" />价格趋势</a>
        <a href="#market-comparison"><Icon name="market" />市场比价</a>
        <a href="#price-records"><Icon name="table" />报价明细</a>
        <a href="#data-notes"><Icon name="info" />数据口径</a>
        <a className={window.location.hash === '#data-management' ? 'nav-overview' : ''} href="#data-management"><Icon name="upload" />数据管理</a>
        <a className={window.location.hash === '#forecasts' ? 'nav-overview' : ''} href="#forecasts"><Icon name="chart" />周度预测</a>
      </nav>
      <div className="sidebar-note"><Icon name="layers" size={22} /><strong>从每周报价，看市场变化</strong><p>比较同商品、同规格、同单位的真实监测价格。</p></div>
      <div className="sidebar-foot">成都 · 农贸市场价格监测</div>
    </aside>
    <div className="workspace">{children}</div>
  </div>;
}
export function Loading({ text = '正在读取价格数据' }: { text?: string }) {
  return <div className="loading-panel" role="status"><span className="loader" />{text}</div>;
}
export function ErrorMessage({ message }: { message?: string }) {
  return message ? <div className="error-message" role="alert"><Icon name="info" />{message}</div> : null;
}
