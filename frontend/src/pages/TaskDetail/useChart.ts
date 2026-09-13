/**
 * 轻量 echarts hook（不引第三方 wrapper）：
 * 挂载时 init → deps 变化时 setOption → ResizeObserver 自适应 → 卸载时 dispose。
 * SSR / 空容器 / 无 canvas 环境（如 jsdom）防御：init 失败时静默降级为无图。
 */

import * as echarts from "echarts";
import type { EChartsOption } from "echarts";
import { useEffect, useRef } from "react";
import type { RefObject } from "react";

/** canvas 2d 能力探测：jsdom / 无 canvas 环境返回 false（echarts init 不抛错但 dispose 会崩，须先探测）。 */
function canvasSupported(): boolean {
  try {
    const canvas = document.createElement("canvas");
    return Boolean(canvas.getContext("2d"));
  } catch {
    return false;
  }
}

export function useChart(
  getOption: () => EChartsOption | null,
  deps: readonly unknown[],
): RefObject<HTMLDivElement> {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);
  const getOptionRef = useRef(getOption);
  getOptionRef.current = getOption;

  // init / ResizeObserver / dispose：仅挂载与卸载时执行一次
  useEffect(() => {
    const el = containerRef.current;
    if (!el || typeof window === "undefined" || !canvasSupported()) return;
    try {
      chartRef.current = echarts.init(el);
    } catch {
      chartRef.current = null; // 环境不支持 canvas：降级为无图，不影响文本信息展示
      return;
    }
    const observer = new ResizeObserver(() => chartRef.current?.resize());
    observer.observe(el);
    return () => {
      observer.disconnect();
      chartRef.current?.dispose();
      chartRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // option 更新
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const option = getOptionRef.current();
    if (option) chart.setOption(option);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return containerRef;
}
