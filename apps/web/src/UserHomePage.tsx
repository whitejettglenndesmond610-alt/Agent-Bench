import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useRef } from "react";
import { Link } from "react-router-dom";

import {
  fetchGlobalLeaderboardOverview,
  fetchWorkspaceComparisons,
  fetchWorkspaceConversations,
  SessionInfo
} from "./api";
import { LoadingState } from "./components";
import { DeltaMark } from "./DeltaMark";
import { deltaForMetric, usePreviousLeaderboardSnapshot } from "./leaderboardDelta";
import { formatRelativeTime, formatUserDate, userQueryKeys } from "./userProduct";

import "./user-home.css";

function prefersReducedMotion(): boolean {
  return typeof window !== "undefined"
    && typeof window.matchMedia === "function"
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** 光晕层鼠标视差：lerp ~0.05 缓动跟随；reduced-motion 时完全不启动。 */
function useHeroParallax(heroRef: React.RefObject<HTMLElement | null>) {
  useEffect(() => {
    const hero = heroRef.current;
    if (!hero || prefersReducedMotion() || typeof requestAnimationFrame !== "function") return;
    const layer = hero.querySelector<HTMLElement>(".hero-aurora");
    if (!layer) return;
    let targetX = 0;
    let targetY = 0;
    let currentX = 0;
    let currentY = 0;
    let frame = 0;
    const onPointerMove = (event: PointerEvent) => {
      const rect = hero.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      targetX = ((event.clientX - rect.left) / rect.width - 0.5) * 36;
      targetY = ((event.clientY - rect.top) / rect.height - 0.5) * 24;
    };
    const tick = () => {
      currentX += (targetX - currentX) * 0.05;
      currentY += (targetY - currentY) * 0.05;
      layer.style.transform = `translate3d(${currentX.toFixed(2)}px, ${currentY.toFixed(2)}px, 0)`;
      frame = requestAnimationFrame(tick);
    };
    hero.addEventListener("pointermove", onPointerMove);
    frame = requestAnimationFrame(tick);
    return () => {
      hero.removeEventListener("pointermove", onPointerMove);
      cancelAnimationFrame(frame);
    };
  }, [heroRef]);
}

/** 内容带入场：IntersectionObserver 一次性 fade；不支持或 reduced-motion 直接可见。 */
function useRevealOnce() {
  const observerRef = useRef<IntersectionObserver | null>(null);
  useEffect(() => () => observerRef.current?.disconnect(), []);
  return useCallback((node: HTMLElement | null) => {
    observerRef.current?.disconnect();
    observerRef.current = null;
    if (!node) return;
    if (typeof IntersectionObserver !== "function" || prefersReducedMotion()) {
      node.classList.add("is-visible");
      return;
    }
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        node.classList.add("is-visible");
        observer.disconnect();
      }
    }, { threshold: 0.15 });
    observerRef.current = observer;
    observer.observe(node);
  }, []);
}

const HERO_ACTIONS = [
  {
    to: "/workspace",
    title: "开始在线对话",
    description: "与榜单 Agent 直接对话，每一轮都留存记录。"
  },
  {
    to: "/compare",
    title: "发起多 Agent 对比",
    description: "同一个问题，看几个 Agent 并排作答。"
  },
  {
    to: "/plaza",
    title: "浏览 Agent 广场",
    description: "按正式榜单挑选值得信任的 Agent。"
  }
] as const;

export function UserHomePage({ session }: { session: SessionInfo }) {
  const userId = session.user_id;
  const heroRef = useRef<HTMLElement | null>(null);
  useHeroParallax(heroRef);
  const revealBand = useRevealOnce();

  const conversationsQuery = useQuery({
    queryKey: userQueryKeys.conversations(userId),
    queryFn: fetchWorkspaceConversations
  });
  const comparisonsQuery = useQuery({
    queryKey: userQueryKeys.comparisons(userId),
    queryFn: () => fetchWorkspaceComparisons(true)
  });
  const leaderboardQuery = useQuery({
    queryKey: ["leaderboard-global-overview"],
    queryFn: fetchGlobalLeaderboardOverview
  });
  const previousSnapshot = usePreviousLeaderboardSnapshot();

  const records = [
    ...(conversationsQuery.data?.items ?? []).map((item) => ({
      id: item.id,
      title: item.title,
      updatedAt: item.updated_at,
      href: `/workspace/${item.id}`,
      type: "对话"
    })),
    ...(comparisonsQuery.data?.items ?? []).map((item) => ({
      id: item.id,
      title: item.title,
      updatedAt: item.updated_at,
      href: `/compare/${item.id}`,
      type: "对比"
    }))
  ].sort((left, right) => Date.parse(right.updatedAt) - Date.parse(left.updatedAt)).slice(0, 3);

  const leader = (leaderboardQuery.data?.agents ?? [])
    .filter((agent) => agent.metrics.overall.rank !== null && agent.metrics.overall.score !== null)
    .sort((left, right) => (left.metrics.overall.rank ?? 999) - (right.metrics.overall.rank ?? 999))[0];
  const leaderDelta = leader
    ? deltaForMetric(previousSnapshot, leader.agent_id, "overall", {
      rank: leader.metrics.overall.rank,
      score: leader.metrics.overall.score
    })
    : null;

  const recordsPending = conversationsQuery.isPending || comparisonsQuery.isPending;
  const showRecords = recordsPending || records.length > 0;
  const showLeader = leaderboardQuery.isPending || Boolean(leader);
  const showBand = showRecords || showLeader;

  return (
    <main className="workspace user-home-page">
      <section ref={heroRef} className="home-hero grain" aria-label="欢迎">
        <div className="hero-aurora" aria-hidden="true" />
        <div className="hero-inner">
          <h1 className="hero-title">{session.display_name ? `${session.display_name}，` : ""}今天想让 Agent 做什么？</h1>
          <p className="hero-sub">从正式榜单选 Agent，在在线工作区完成问题、并行比较和个人复盘。</p>
          <nav className="hero-actions" aria-label="快捷操作">
            {HERO_ACTIONS.map((action) => (
              <Link key={action.to} className="hero-action" to={action.to}>
                <strong>{action.title}</strong>
                <span>{action.description}</span>
                <i className="hero-action-arrow" aria-hidden="true">→</i>
              </Link>
            ))}
          </nav>
        </div>
      </section>

      {showBand && (
        <section ref={revealBand} className="home-band" aria-label="动态一览">
          {showRecords && (
            <div className="band-records">
              <header>
                <h2>最近记录</h2>
                <Link to="/records">查看全部</Link>
              </header>
              {recordsPending ? (
                <LoadingState label="正在同步" compact />
              ) : (
                <ol>
                  {records.map((record) => (
                    <li key={`${record.type}-${record.id}`}>
                      <Link to={record.href}>
                        <i className={`record-kind is-${record.type === "对话" ? "chat" : "compare"}`} aria-hidden="true">
                          {record.type === "对话" ? "对" : "比"}
                        </i>
                        <strong>{record.title}</strong>
                        <time dateTime={record.updatedAt} title={formatUserDate(record.updatedAt, true)}>
                          {formatRelativeTime(record.updatedAt)}
                        </time>
                      </Link>
                    </li>
                  ))}
                </ol>
              )}
            </div>
          )}

          {showLeader && (
            <div className="band-leader-wrap">
              {leaderboardQuery.isPending || !leader ? (
                <LoadingState label="正在读取榜首" compact />
              ) : (
                <Link className="band-leader" to="/leaderboard">
                  <span className="band-leader-rank">{String(leader.metrics.overall.rank ?? 1).padStart(2, "0")}</span>
                  <span className="band-leader-main">
                    <strong>{leader.agent_name}</strong>
                    <small>当前榜首 · {leader.provider}{leader.version === null ? "" : ` · V${leader.version}`}</small>
                  </span>
                  <DeltaMark delta={leaderDelta} />
                  <span className="band-leader-score">{leader.metrics.overall.score?.toFixed(2) ?? "--"}</span>
                </Link>
              )}
            </div>
          )}
        </section>
      )}

      <aside className="persistence-notice">
        <strong>{session.is_shared ? session.role === "operator" ? "共享管理身份提醒" : "共享浏览器提醒" : "个人记录已持续保存"}</strong>
        <span>{session.is_shared
          ? session.role === "operator"
            ? "当前记录由使用同一评测管理密码的会话共享。请使用具名账号保存个人模板和私人笔记。"
            : "当前记录绑定此浏览器中的共享身份。清理站点数据或换用浏览器后可能无法继续访问，请勿在共享设备写入敏感私人笔记。"
          : "会话、收藏、模板与私人标注保存在你的服务端账号中，换用已登录设备仍可继续。"}</span>
      </aside>
    </main>
  );
}
