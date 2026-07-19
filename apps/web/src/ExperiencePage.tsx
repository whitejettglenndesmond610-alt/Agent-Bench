import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { fetchWorkspaceExperienceTask, fetchWorkspaceExperienceTasks, WorkspaceExperienceCategory } from "./api";
import { LoadingState } from "./components";
import { userQueryKeys } from "./userProduct";

const categoryLabels: Record<WorkspaceExperienceCategory, string> = {
  analysis: "分析决策",
  retrieval_citations: "检索与引用",
  structured_extraction: "结构化提取",
  writing_instruction_following: "写作与指令遵循",
  data_reasoning: "数据推理",
  long_context_synthesis: "长文本综合"
};
const difficultyLabels = { beginner: "入门", intermediate: "进阶", advanced: "挑战" } as const;

export function ExperiencePage({ userId }: { userId: string }) {
  const { taskSlug } = useParams();
  const [category, setCategory] = useState("all");
  const [difficulty, setDifficulty] = useState("all");
  const [mode, setMode] = useState("all");
  const tasksQuery = useQuery({ queryKey: userQueryKeys.tasks(userId), queryFn: fetchWorkspaceExperienceTasks });
  const detailQuery = useQuery({
    queryKey: userQueryKeys.task(userId, taskSlug ?? "none"),
    queryFn: () => fetchWorkspaceExperienceTask(taskSlug!),
    enabled: Boolean(taskSlug),
    retry: false
  });
  const tasks = (tasksQuery.data ?? []).filter((task) =>
    (category === "all" || task.category === category)
    && (difficulty === "all" || task.difficulty === difficulty)
    && (mode === "all" || task.recommended_mode === mode)
  );
  const task = detailQuery.data ?? tasksQuery.data?.find((item) => item.slug === taskSlug);

  return (
    <main className="workspace experience-page">
      <header className="section-header"><div><p className="eyebrow">PRACTICE, NOT RANKING</p><h1>体验任务</h1><p>用六类典型工作情境了解 Agent 的回答风格和协作方式。</p></div></header>
      <aside className="experience-policy"><strong>体验结果不影响正式排行榜</strong><span>这里的任务只用于练习与产品体验，不会进入正式评测；页面不提供参考答案、Judge 提示词、评分或裁判数据。</span></aside>
      {taskSlug ? detailQuery.isPending ? <LoadingState label="正在读取体验任务" /> : detailQuery.error || !task ? <div className="table-state is-error" role="alert">{detailQuery.error?.message ?? "体验任务不存在"}</div> : (
        <article className="experience-detail">
          <Link className="back-link" to="/experience">← 返回全部任务</Link>
          <header><div><span>{categoryLabels[task.category]} · {difficultyLabels[task.difficulty]}</span><h2>{task.title}</h2><p>{task.summary}</p></div><b>{task.recommended_mode === "chat" ? "推荐单 Agent" : `推荐 ${task.min_agents}-${task.max_agents} 个 Agent`}</b></header>
          <section><span>任务提示词</span><p>{task.prompt}</p></section>
          <footer><Link className="button" to={`/workspace?task=${task.slug}`}>用单 Agent 开始</Link><Link className="button is-ghost" to={`/compare?task=${task.slug}`}>用多 Agent 对比</Link></footer>
          <small>此任务 ranking_eligible = false，不展示任何隐藏参考或 Judge 信息。</small>
        </article>
      ) : <>
        <section className="experience-filters" aria-label="体验任务筛选">
          <label><span>类别</span><select aria-label="筛选任务类别" value={category} onChange={(event) => setCategory(event.target.value)}><option value="all">全部类别</option>{Object.entries(categoryLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
          <label><span>难度</span><select aria-label="筛选任务难度" value={difficulty} onChange={(event) => setDifficulty(event.target.value)}><option value="all">全部难度</option><option value="beginner">入门</option><option value="intermediate">进阶</option><option value="advanced">挑战</option></select></label>
          <label><span>推荐模式</span><select aria-label="筛选推荐模式" value={mode} onChange={(event) => setMode(event.target.value)}><option value="all">全部模式</option><option value="chat">单 Agent 对话</option><option value="comparison">多 Agent 对比</option></select></label>
          <strong>{tasks.length} 个任务</strong>
        </section>
        {tasksQuery.isPending ? <LoadingState label="正在读取体验任务" /> : tasksQuery.error ? <div className="table-state is-error" role="alert">{tasksQuery.error.message}</div> : tasks.length ? <section className="experience-task-grid">{tasks.map((item, index) => (
          <article key={item.slug}>
            <header><b>{String(index + 1).padStart(2, "0")}</b><span>{categoryLabels[item.category]}</span><small>{difficultyLabels[item.difficulty]}</small></header>
            <h2><Link to={`/experience/${item.slug}`}>{item.title}</Link></h2>
            <p>{item.summary}</p>
            <dl><div><dt>推荐</dt><dd>{item.recommended_mode === "chat" ? "单 Agent" : "多 Agent"}</dd></div><div><dt>范围</dt><dd>{item.min_agents === item.max_agents ? item.min_agents : `${item.min_agents}-${item.max_agents}`} 个</dd></div></dl>
            <footer><Link to={`/workspace?task=${item.slug}`}>单 Agent 开始</Link><Link to={`/compare?task=${item.slug}`}>多 Agent 对比</Link><Link to={`/experience/${item.slug}`}>查看任务</Link></footer>
          </article>
        ))}</section> : <section className="empty-state"><span>NO MATCH</span><h2>没有符合条件的体验任务</h2><p>调整类别、难度或推荐模式筛选。</p></section>}
      </>}
    </main>
  );
}
