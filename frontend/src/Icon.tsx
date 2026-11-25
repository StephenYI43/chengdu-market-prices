import type { CSSProperties } from 'react';

const paths: Record<string, string> = {
  alert: 'M12 3 2 21h20L12 3ZM12 9v5m0 3h.01',
  upload: 'M12 16V4m-4 4 4-4 4 4M4 15v6h16v-6',
  leaf: 'M20 4c-9-1-15 3-15 9a6 6 0 0 0 6 6c6 0 10-6 9-15ZM4 21l11-11M5 13h7v6',
  grid: 'M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z',
  chart: 'M4 3v17h17M7 14l4-5 4 3 5-7',
  market: 'M3 10h18M5 10v11h14V10M3 10l2-7h14l2 7M9 21v-7h6v7',
  table: 'M3 4h18v16H3zM3 10h18M9 4v16',
  calendar: 'M4 5h16v16H4zM8 3v4M16 3v4M4 11h16',
  refresh: 'M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0 1 12-1l2 3M4 15l2 3a7 7 0 0 0 12-1',
  download: 'M12 3v12m-4-4 4 4 4-4M4 16v5h16v-5',
  info: 'M12 11v6M12 7h.01M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0',
  layers: 'm12 3 10 5-10 5L2 8l10-5ZM2 12l10 5 10-5M2 16l10 5 10-5',
  check: 'm5 12 4 4L19 6', filter: 'M3 4h18l-7 8v7l-4 2v-9L3 4Z', chevron: 'm9 5 7 7-7 7',
};
export function Icon({ name, size = 20, style }: { name: string; size?: number; style?: CSSProperties }) {
  return <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" style={style}><path d={paths[name] ?? paths.info} /></svg>;
}
