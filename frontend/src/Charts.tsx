import { useEffect, useMemo, useRef } from 'react';
import { init, use, type ComposeOption, type EChartsType } from 'echarts/core';
import { LineChart, BarChart, type LineSeriesOption, type BarSeriesOption } from 'echarts/charts';
import { GridComponent, TooltipComponent, LegendComponent, DataZoomComponent, AriaComponent, type GridComponentOption, type TooltipComponentOption, type LegendComponentOption, type DataZoomComponentOption, type AriaComponentOption } from 'echarts/components';
import { SVGRenderer } from 'echarts/renderers';
import { money } from './api';
import type { Comparison, TrendPoint } from './types';

use([LineChart, BarChart, GridComponent, TooltipComponent, LegendComponent, DataZoomComponent, AriaComponent, SVGRenderer]);
type Option = ComposeOption<LineSeriesOption | BarSeriesOption | GridComponentOption | TooltipComponentOption | LegendComponentOption | DataZoomComponentOption | AriaComponentOption>;

function Chart({ option, label, height }: { option: Option; label: string; height: number }) {
  const node = useRef<HTMLDivElement>(null);
  const chart = useRef<EChartsType | null>(null);
  useEffect(() => {
    if (!node.current) return;
    const instance = init(node.current, undefined, { renderer: 'svg' });
    chart.current = instance;
    const observer = new ResizeObserver(() => instance.resize());
    observer.observe(node.current);
    return () => { observer.disconnect(); instance.dispose(); chart.current = null; };
  }, []);
  useEffect(() => { chart.current?.setOption(option, { notMerge: true }); }, [option]);
  return <div className="chart" ref={node} role="img" aria-label={label} style={{ height }} />;
}

export function TrendChart({ points, dates, unit }: { points: TrendPoint[]; dates: string[]; unit: string }) {
  const option = useMemo<Option>(() => {
    const byDate = new Map(points.map(point => [point.quote_date, point]));
    const series = (field: 'mean_price' | 'min_price' | 'max_price') => dates.map(day => {
      const value = byDate.get(day)?.[field];
      return value == null ? null : Number(value);
    });
    return {
      animationDuration: 250, color: ['#087f5b', '#8aa9b8', '#cda777'], textStyle: { fontFamily: 'inherit' },
      aria: { enabled: true, label: { description: `同一商品规格的周度价格，单位${unit}。缺期显示断点，数据表提供具体数值。` } },
      grid: { left: 48, right: 22, top: 54, bottom: 66 },
      legend: { top: 6, left: 4, itemWidth: 16, itemHeight: 8, textStyle: { fontSize: 14, color: '#5f706a' } },
      tooltip: { trigger: 'axis', renderMode: 'richText', valueFormatter: value => value == null ? '无报价' : `${money(String(value))} ${unit}` },
      xAxis: { type: 'category', data: dates, boundaryGap: false, axisLine: { lineStyle: { color: '#dfe7e2' } }, axisTick: { show: false }, axisLabel: { color: '#728078', fontSize: 12, hideOverlap: true, formatter: (day: string) => dates.length > 40 ? day.slice(0, 7) : day.slice(5).replace('-', '/') } },
      yAxis: { type: 'value', scale: true, axisLabel: { color: '#728078', fontSize: 12 }, splitLine: { lineStyle: { color: '#edf1ee', type: 'dashed' } } },
      dataZoom: [{ type: 'inside', filterMode: 'none' }, { type: 'slider', bottom: 6, height: 18, borderColor: '#e5ebe7', fillerColor: '#d7ede3', handleStyle: { color: '#087f5b' }, showDetail: false }],
      series: [
        { type: 'line', name: '市场均价', data: series('mean_price'), connectNulls: false, smooth: false, symbol: 'circle', symbolSize: 6, lineStyle: { width: 3 }, areaStyle: { color: '#087f5b', opacity: 0.07 }, emphasis: { focus: 'series' } },
        { type: 'line', name: '最低价', data: series('min_price'), connectNulls: false, symbol: 'none', lineStyle: { width: 1.5, type: 'dashed' } },
        { type: 'line', name: '最高价', data: series('max_price'), connectNulls: false, symbol: 'none', lineStyle: { width: 1.5, type: 'dashed' } },
      ],
    };
  }, [points, dates, unit]);
  return <Chart option={option} label="商品周度价格趋势，可查看下方趋势数据表" height={370} />;
}
export function MarketChart({ comparison }: { comparison: Comparison }) {
  const option = useMemo<Option>(() => ({
    animationDuration: 250, textStyle: { fontFamily: 'inherit' },
    aria: { enabled: true, label: { description: '同一商品规格在所选日期的各市场报价。无报价不显示价格柱。' } },
    grid: { left: 178, right: 52, top: 12, bottom: 22 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, renderMode: 'richText', valueFormatter: value => value == null ? '无报价' : `${money(String(value))} ${comparison.product.unit}` },
    xAxis: { type: 'value', axisLabel: { color: '#728078', fontSize: 12 }, splitLine: { lineStyle: { color: '#edf1ee', type: 'dashed' } } },
    yAxis: { type: 'category', inverse: true, data: comparison.items.map(item => item.market), axisTick: { show: false }, axisLine: { show: false }, axisLabel: { width: 160, overflow: 'truncate', color: '#53665d', fontSize: 14 } },
    series: [{ type: 'bar', name: '市场报价', barMaxWidth: 12, data: comparison.items.map(item => ({ value: item.price == null ? null : Number(item.price), itemStyle: { color: item.rank === 1 ? '#087f5b' : '#87b4a3', borderRadius: [0, 3, 3, 0] } })), label: { show: true, position: 'right', color: '#42594e', fontSize: 12, formatter: params => params.value == null ? '' : money(String(params.value)) } }],
  }), [comparison]);
  return <Chart option={option} label="各市场报价比较，具体价格和缺失状态见下方市场数据表" height={Math.max(240, comparison.items.length * 25 + 34)} />;
}
