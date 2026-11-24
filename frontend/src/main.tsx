import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import './style.css';

function App() {
  return <main>
    <p className="eyebrow">成都市农贸市场价格</p>
    <h1>数据分析与预测平台</h1>
    <p>当前已建立项目骨架。历史分析、市场比价、流式监控和周度预测按实施计划逐步接入。</p>
    <section>
      <h2>数据口径</h2>
      <p>以真实周度监测报价为基础，比较同商品、同规格、同单位的市场价格。</p>
      <a href="http://127.0.0.1:18880/docs">查看当前接口文档</a>
    </section>
  </main>;
}

createRoot(document.getElementById('root')!).render(<StrictMode><App/></StrictMode>);
