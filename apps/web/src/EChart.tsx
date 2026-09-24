import { LineChart, RadarChart } from "echarts/charts";
import { AriaComponent, GridComponent, LegendComponent, TooltipComponent } from "echarts/components";
import { EChartsCoreOption, init, use } from "echarts/core";
import { SVGRenderer } from "echarts/renderers";
import { useEffect, useRef } from "react";

use([RadarChart, LineChart, TooltipComponent, LegendComponent, GridComponent, AriaComponent, SVGRenderer]);

export function EChart({
  option,
  className,
  ariaLabel,
  fallbackHeight = 320
}: {
  option: EChartsCoreOption;
  className?: string;
  ariaLabel: string;
  fallbackHeight?: number;
}) {
  const chartRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const element = chartRef.current;
    if (!element) return;
    const chart = init(element, undefined, {
      renderer: "svg",
      width: element.clientWidth || 720,
      height: element.clientHeight || fallbackHeight
    });
    const reducedMotion = typeof window.matchMedia === "function"
      && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    chart.setOption({ ...option, animation: !reducedMotion });
    const resize = () => chart.resize();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(resize);
    if (observer) observer.observe(element);
    else window.addEventListener("resize", resize);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", resize);
      chart.dispose();
    };
  }, [option, fallbackHeight]);

  return <div ref={chartRef} className={["echart-frame", className].filter(Boolean).join(" ")} role="img" aria-label={ariaLabel} />;
}
