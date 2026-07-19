import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import {
  fetchGlobalLeaderboardOverview,
  fetchWorkspaceAgents,
  fetchWorkspaceComparisons,
  fetchWorkspaceConversations,
  fetchWorkspaceExperienceTasks,
  fetchWorkspacePromptTemplates,
  SessionInfo
} from "./api";
import { FavoriteAgentButton, formatUserDate, userQueryKeys } from "./userProduct";

function SectionState({ loading, error, empty }: { loading: boolean; error?: Error | null; empty: string }) {
  if (loading) return <p className="user-section-state">正在同步...</p>;
  if (error) return <p className="user-section-state is-error" role="alert">{error.message}</p>;
  return <p className="user-section-state">{empty}</p>;
}

export function UserHomePage({ session }: { session: SessionInfo }) {
  const userId = session.user_id;
  const conversationsQuery = useQuery({
    queryKey: userQueryKeys.conversations(userId),
    queryFn: fetchWorkspaceConversations
  });
  const comparisonsQuery = useQuery({
    queryKey: userQueryKeys.comparisons(userId),
    queryFn: () => fetchWorkspaceComparisons(true)
  });
  const agentsQuery = useQuery({ queryKey: userQueryKeys.agents(userId), queryFn: fetchWorkspaceAgents });
  const templatesQuery = useQuery({
    queryKey: userQueryKeys.templates(userId),
    queryFn: fetchWorkspacePromptTemplates
  });
  const tasksQuery = useQuery({ queryKey: userQueryKeys.tasks(userId), queryFn: fetchWorkspaceExperienceTasks });
  const leaderboardQuery = useQuery({
    queryKey: ["leaderboard-global-overview"],
    queryFn: fetchGlobalLeaderboardOverview
  });

  const records = [
    ...(conversationsQuery.data?.items ?? []).map((item) => ({
      id: item.id,
      title: item.title,
      meta: `${item.agent.name} · ${item.last_turn_no} 轮`,
      updatedAt: item.updated_at,
      href: `/workspace/${item.id}`,
      type: "对话"
    })),
    ...(comparisonsQuery.data?.items ?? []).map((item) => ({
      id: item.id,
      title: item.title,
      meta: `${item.items.length} 个 Agent · ${item.counts.succeeded} 个成功`,
      updatedAt: item.updated_at,
      href: `/compare/${item.id}`,
      type: "对比"
    }))
  ].sort((left, right) => Date.parse(right.updatedAt) - Date.parse(left.updatedAt)).slice(0, 5);
  const favorites = (agentsQuery.data ?? []).filter((agent) => agent.visibility !== "private" && agent.is_favorite).slice(0, 4);
  const templates = (templatesQuery.data?.items ?? []).slice(0, 3);
  const tasks = (tasksQuery.data ?? []).slice(0, 3);
  const leaders = (leaderboardQuery.data?.agents ?? [])
    .filter((agent) => agent.metrics.overall.rank !== null)
    .sort((left, right) => (left.metrics.overall.rank ?? 999) - (right.metrics.overall.rank ?? 999))
    .slice(0, 5);

  return (
    <main className="workspace user-home-page">
      <header className="user-hero">
        <div>
          <p className="eyebrow">YOUR AGENT WORKSPACE</p>
          <h1>{session.display_name ? `${session.display_name}，` : ""}今天想让 Agent 做什么？</h1>
          <p>从正式榜单选 Agent，在在线工作区完成问题、并行比较和个人复盘。</p>
        </div>
        <nav aria-label="快捷操作">
          <Link className="button" to="/workspace">开始在线对话</Link>
          <Link className="button is-ghost" to="/compare">发起多 Agent 对比</Link>
          <Link className="button is-ghost" to="/plaza">浏览 Agent 广场</Link>
        </nav>
      </header>

      <aside className="persistence-notice">
        <strong>{session.is_shared ? session.role === "operator" ? "共享管理身份提醒" : "共享浏览器提醒" : "个人记录已持续保存"}</strong>
        <span>{session.is_shared
          ? session.role === "operator"
            ? "当前记录由使用同一评测管理密码的会话共享。请使用具名账号保存个人模板和私人笔记。"
            : "当前记录绑定此浏览器中的共享身份。清理站点数据或换用浏览器后可能无法继续访问，请勿在共享设备写入敏感私人笔记。"
          : "会话、收藏、模板与私人标注保存在你的服务端账号中，换用已登录设备仍可继续。"}</span>
      </aside>

      <section className="user-home-grid">
        <article className="user-product-panel recent-records-panel">
          <header><div><span>RECENT ACTIVITY</span><h2>最近记录</h2></div><Link to="/records">查看全部</Link></header>
          {records.length ? <ol>{records.map((record) => (
            <li key={`${record.type}-${record.id}`}>
              <Link to={record.href}><strong>{record.title}</strong><small>{record.type} · {record.meta}</small></Link>
              <time dateTime={record.updatedAt}>{formatUserDate(record.updatedAt)}</time>
            </li>
          ))}</ol> : <SectionState
            loading={conversationsQuery.isPending || comparisonsQuery.isPending}
            error={(conversationsQuery.error ?? comparisonsQuery.error) as Error | null}
            empty="还没有使用记录，从一次在线对话开始。"
          />}
        </article>

        <article className="user-product-panel favorite-agents-panel">
          <header><div><span>FAVORITES</span><h2>收藏的 Agent</h2></div><Link to="/plaza">前往广场</Link></header>
          {favorites.length ? <div className="home-agent-list">{favorites.map((agent) => (
            <section key={agent.agent_id}>
              <Link to={`/plaza/${agent.agent_id}`}><i>{agent.name.slice(0, 2).toUpperCase()}</i><span><strong>{agent.name}</strong><small>{agent.provider} · V{agent.version}</small></span></Link>
              <FavoriteAgentButton userId={userId} agent={agent} />
            </section>
          ))}</div> : <SectionState loading={agentsQuery.isPending} error={agentsQuery.error} empty="收藏常用 Agent 后会显示在这里。" />}
        </article>

        <article className="user-product-panel current-leaders-panel">
          <header><div><span>OFFICIAL LEADERS</span><h2>综合榜领先者</h2></div><Link to="/leaderboard">完整榜单</Link></header>
          {leaders.length ? <ol>{leaders.map((agent) => (
            <li key={agent.agent_id}>
              <b>{String(agent.metrics.overall.rank).padStart(2, "0")}</b>
              <Link to={`/plaza/${agent.agent_id}`}><strong>{agent.agent_name}</strong><small>{agent.provider} · V{agent.version}</small></Link>
              <span>{agent.metrics.overall.score?.toFixed(2)}</span>
            </li>
          ))}</ol> : <SectionState loading={leaderboardQuery.isPending} error={leaderboardQuery.error} empty="当前还没有正式综合成绩。" />}
        </article>

        <article className="user-product-panel home-templates-panel">
          <header><div><span>PROMPT LIBRARY</span><h2>最近模板</h2></div><Link to="/templates">管理模板</Link></header>
          {templates.length ? <ol>{templates.map((template) => (
            <li key={template.id}><div><strong>{template.name}</strong><p>{template.content}</p></div><Link to={`/workspace?template=${template.id}`}>用于对话</Link></li>
          ))}</ol> : <SectionState loading={templatesQuery.isPending} error={templatesQuery.error} empty="创建模板，复用你常用的纯文本提示词。" />}
        </article>

        <article className="user-product-panel home-tasks-panel">
          <header><div><span>PRACTICE LAB</span><h2>精选体验任务</h2></div><Link to="/experience">全部任务</Link></header>
          {tasks.length ? <div>{tasks.map((task) => (
            <section key={task.slug}><span>{task.difficulty}</span><strong>{task.title}</strong><p>{task.summary}</p><Link to={task.recommended_mode === "chat" ? `/workspace?task=${task.slug}` : `/compare?task=${task.slug}`}>开始体验</Link></section>
          ))}</div> : <SectionState loading={tasksQuery.isPending} error={tasksQuery.error} empty="体验任务暂不可用。" />}
          <footer>体验任务不参与正式排名，不展示参考答案或 Judge 数据。</footer>
        </article>
      </section>
    </main>
  );
}
