import { useEffect, useState } from 'react';

export function endpoint(path: string, values: Record<string, string | number | boolean | undefined>) {
  const query = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== undefined && value !== '') query.set(key, String(value));
  });
  return `/api/v1/${path}?${query}`;
}

export async function readJson<T>(url: string, signal: AbortSignal): Promise<T> {
  const response = await fetch(url, { signal, cache: 'no-store' });
  const body = await response.json();
  if (!response.ok) {
    const detail = body.detail;
    throw new Error(typeof detail?.message === 'string' ? detail.message : `查询未完成（${response.status}），请检查筛选条件后重试`);
  }
  return body as T;
}

export function useResource<T>(url: string | null, revision = 0) {
  const key = `${revision}:${url}`;
  const [state, setState] = useState<{ key: string; data?: T; error?: string }>({ key: '' });
  useEffect(() => {
    if (!url) return;
    const controller = new AbortController();
    readJson<T>(url, controller.signal).then(
      data => { if (!controller.signal.aborted) setState({ key, data }); },
      error => { if (!controller.signal.aborted) setState({ key, error: error instanceof TypeError ? '无法连接本机服务，请确认服务已启动后重试' : error instanceof Error ? error.message : '服务暂不可用，请重试' }); },
    );
    return () => controller.abort();
  }, [url, key]);
  return state.key === key ? { ...state, loading: false } : { loading: Boolean(url), data: undefined, error: undefined };
}

export const money = (value: string | null | undefined) => value == null ? '—' : Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 4 });
export const percent = (value: number | null | undefined) => value == null ? '—' : `${value > 0 ? '+' : ''}${(value * 100).toFixed(2)}%`;
export const flagNames: Record<string, string> = {
  large_weekly_change: '周变动较大', high_vs_peer_median: '高于同商品中位数', low_vs_peer_median: '低于同商品中位数',
};
