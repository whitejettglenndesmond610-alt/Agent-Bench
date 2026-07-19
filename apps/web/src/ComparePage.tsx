import { useQuery } from "@tanstack/react-query";
import { RadarChart } from "echarts/charts";
import { AriaComponent, LegendComponent, TooltipComponent } from "echarts/components";
import { init, use } from "echarts/core";
import { SVGRenderer } from "echarts/renderers";
import { CSSProperties, useEffect, useRef } from "react";
import { Link, useSearchParams } from "react-router-dom";

import {
  fetchGlobalLeaderboardComparison,
  GlobalLeaderboardMetric,
  GlobalOverviewAgent
} from "./api";
import { abilityMetrics, leaderboardMetrics, metricByKey } from "./metrics";

use([RadarChart, TooltipComponent, LegendComponent, AriaComponent, SVGRenderer]);

const colors = ["#4f46e5", "#0891b2", "#7c3aed", "#e11d48", "#059669"];

function AbilityRadar({ agents }: { agents: GlobalOverviewAgent[] }) {
  const chartRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const element = chartRef.current;
    if (!element) return;
    const chart = init(element, undefined, {
      renderer: "svg",
      width: element.clientWidth || 720,
      height: element.clientHeight || 385
    });
    const reducedMotion = typeof window.matchMedia === "function"
      && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    chart.setOption({
      animation: !reducedMotion,
      color: colors,
      aria: { enabled: true, decal: { show: true } },
      tooltip: { trigger: "item" },
      legend: {
        bottom: 0,
        textStyle: { color: "#64748b", fontSize: 10 },
        data: agents.map((agent) => `${agent.agent_name} · V${agent.version}`)
      },
      radar: {
        center: ["50%", "43%"],
        radius: "62%",
        splitNumber: 5,
        indicator: abilityMetrics.map((ability) => ({ name: ability.label, max: 100, min: 0 })),
        axisName: { color: "#64748b", fontSize: 10 },
        splitArea: { areaStyle: { color: ["#ffffff", "#f4f6ff"] } },
        splitLine: { lineStyle: { color: "#dce3ee" } },
        axisLine: { lineStyle: { color: "#cbd5e1" } }
      },
      series: [{
        type: "radar",
        symbolSize: 5,
        lineStyle: { width: 2 },
        areaStyle: { opacity: 0.12 },
        data: agents.map((agent) => ({
          name: `${agent.agent_name} · V${agent.version}`,
          value: abilityMetrics.map((ability) => agent.metrics[ability.key].score)
        }))
      }]
    });
    const resize = () => chart.resize();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(resize);
    if (observer) observer.observe(element);
    else window.addEventListener("resize", resize);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", resize);
      chart.dispose();
    };
  }, [agents]);

  return <div ref={chartRef} className="ability-radar" role="img" aria-label="Agent 五项能力雷达图，坐标范围 0 到 100" />;
}

function metricRows(agents: GlobalOverviewAgent[]) {
  const score = (agent: GlobalOverviewAgent, metric: GlobalLeaderboardMetric) =>
    agent.metrics[metric].score?.toFixed(2) ?? "—";
  const quality = (agent: GlobalOverviewAgent) => {
    const result = agent.metrics.overall.score !== null
      ? agent.metrics.overall
       : abilityMetrics.map((ability) => agent.metrics[ability.key]).find((item) => item.score !== null);
    return result;
  };
  return [
    ...leaderboardMetrics.map((metric) => ({ group: metric.key === "overall" ? "总览" : "能力", label: metric.label, values: agents.map((agent) => score(agent, metric.key)) })),
    { group: "稳定性", label: "最近评测成功率", values: agents.map((agent) => quality(agent)?.success_rate === null || quality(agent)?.success_rate === undefined ? "—" : `${quality(agent)?.success_rate?.toFixed(2)}%`) },
    { group: "稳定性", label: "最近评测异常率", values: agents.map((agent) => quality(agent)?.anomaly_rate === null || quality(agent)?.anomaly_rate === undefined ? "—" : `${quality(agent)?.anomaly_rate?.toFixed(2)}%`) },
    { group: "观测", label: "最近评测 HTTP 调用", values: agents.map((agent) => String(quality(agent)?.http_call_count ?? "—")) }
  ];
}

export function ComparePage() {
  const [parameters] = useSearchParams();
  const agentIds = (parameters.get("agents") ?? "").split(",").filter(Boolean);
  const validSelection = agentIds.length >= 2 && agentIds.length <= 5
    && new Set(agentIds).size === agentIds.length;
  const comparisonQuery = useQuery({
    queryKey: ["leaderboard-global-compare", agentIds],
    queryFn: () => fetchGlobalLeaderboardComparison(agentIds),
    enabled: validSelection,
    retry: false
  });

  if (!validSelection) {
    return <main className="workspace"><div className="table-state is-error">对比参数无效，请返回榜单重新选择 2 至 5 个 Agent。</div></main>;
  }
  if (comparisonQuery.isPending) {
    return <main className="workspace"><div className="leaderboard-loading"><span className="status-dot" />正在构建全局对比</div></main>;
  }
  if (comparisonQuery.error || !comparisonQuery.data) {
    return <main className="workspace"><div className="table-state is-error">{comparisonQuery.error?.message ?? "对比数据读取失败"}</div></main>;
  }

  const overview = comparisonQuery.data;
  const agents = overview.agents;
  const radarAgents = agents.filter((agent) => abilityMetrics.every((ability) => agent.metrics[ability.key].score !== null));
  const benchmarks = Array.from(new Map(
    agents.flatMap((agent) => abilityMetrics.flatMap((ability) =>
      agent.metrics[ability.key].benchmark_scores.map((benchmark) => [
        `${ability.key}:${benchmark.benchmark_id}`,
        { ...benchmark, metric: ability.key }
      ] as const)
    ))
  ).values());
  const gridStyle = { gridTemplateColumns: `180px repeat(${agents.length}, minmax(170px, 1fr))` };

  return (
    <main className="workspace compare-workspace">
      <Link className="back-link" to="/leaderboard">← 返回能力总览</Link>
      <header className="section-header compare-header"><div><p className="eyebrow">GLOBAL PROTOCOL COMPARISON</p><h1>Agent 横向对比</h1><p>各维度采用当前兼容协议下的最新正式成绩，缺失能力不按 0 分处理。</p></div><aside><span>GLOBAL RESULT</span><strong>{agents.length} 个 Agent</strong><small>{overview.protocols.filter((protocol) => protocol.protocol_hash).length} 项有效协议</small></aside></header>

      <section className="compare-agent-strip" style={gridStyle}><div className="compare-axis-label"><span>对比对象</span><small>颜色与雷达图一致</small></div>{agents.map((agent, index) => <article key={agent.agent_id} style={{ "--agent-color": colors[index] } as CSSProperties}><i /><div><strong>{agent.agent_name}</strong><small>{agent.provider} · V{agent.version}</small></div><b>{agent.metrics.overall.rank === null ? "—" : `#${String(agent.metrics.overall.rank).padStart(2, "0")}`}</b></article>)}</section>

      <section className="compare-primary-grid">
        <article className="radar-panel"><header><div><p className="eyebrow">ABILITY SHAPE</p><h2>五项能力雷达</h2></div><span>固定范围 0—100</span></header>{radarAgents.length ? <AbilityRadar agents={radarAgents} /> : <div className="compare-radar-empty">所选 Agent 尚未同时完成五项专项评测，暂不绘制雷达图。</div>}</article>
        <article className="compare-scorecards"><header><p className="eyebrow">TOTAL SCORE</p><h2>综合分</h2></header>{agents.map((agent, index) => <div key={agent.agent_id}><span style={{ background: colors[index] }} /><strong>{agent.metrics.overall.score?.toFixed(2) ?? "—"}</strong><p>{agent.agent_name}</p><small>{agent.metrics.overall.rank === null ? "尚未完成综合评测" : `综合排名 #${agent.metrics.overall.rank}`}</small></div>)}</article>
      </section>

      <section className="comparison-matrix">
        <header style={gridStyle}><span>指标</span>{agents.map((agent) => <strong key={agent.agent_id}>{agent.agent_name}</strong>)}</header>
        {metricRows(agents).map((row) => <div style={gridStyle} key={`${row.group}-${row.label}`}><span><small>{row.group}</small>{row.label}</span>{row.values.map((value, index) => <b key={agents[index].agent_id}>{value}</b>)}</div>)}
      </section>

      <section className="benchmark-comparison"><header><div><p className="eyebrow">BENCHMARK DETAIL</p><h2>专项 Benchmark 分数</h2></div><small>仅展示当前专项协议中的正式成绩</small></header><div className="comparison-matrix compact"><header style={gridStyle}><span>Benchmark</span>{agents.map((agent) => <strong key={agent.agent_id}>{agent.agent_name}</strong>)}</header>{benchmarks.length ? benchmarks.map((benchmark) => <div style={gridStyle} key={`${benchmark.metric}:${benchmark.benchmark_id}`}><span><small>{benchmark.language.toUpperCase()} / {metricByKey[benchmark.metric].label}</small>{benchmark.name}</span>{agents.map((agent) => <b key={agent.agent_id}>{agent.metrics[benchmark.metric].benchmark_scores.find((item) => item.benchmark_id === benchmark.benchmark_id)?.score.toFixed(2) ?? "—"}</b>)}</div>) : <div className="comparison-empty-row">暂无可比较的 Benchmark 明细。</div>}</div></section>

      <footer className="leaderboard-footnote"><span>COMPARISON POLICY</span><p>雷达图仅纳入五项能力都有正式成绩的 Agent，绝不使用 0 替代缺失成绩。不同维度可来自不同 Run，但每个维度内部必须使用相同协议。</p></footer>
    </main>
  );
}
