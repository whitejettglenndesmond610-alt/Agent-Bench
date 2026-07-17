import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { lazy, Suspense } from "react";
import { Link, Navigate, NavLink, Route, Routes } from "react-router-dom";

import { fetchPlatformMeta, fetchSession, login, logout, SessionInfo, SessionRole } from "./api";
import { AgentsPage } from "./AgentsPage";
import { LoginPage } from "./LoginPage";
import { LeaderboardPage } from "./LeaderboardPage";
import { abilityMetrics } from "./metrics";
import { RunsPage } from "./RunsPage";

const unifiedCapabilityRows = abilityMetrics.map((metric) => ({
  label: metric.label,
  share: "20%",
  tone: metric.tone
}));

const ComparePage = lazy(() =>
  import("./ComparePage").then((module) => ({ default: module.ComparePage }))
);

function Dashboard() {
  const platform = useQuery({
    queryKey: ["platform-meta"],
    queryFn: fetchPlatformMeta,
    refetchInterval: 30_000,
    retry: 1
  });

  const online = platform.data?.status === "ready";

  return (
    <main className="workspace dashboard-workspace">
      <header className="section-header dashboard-header">
        <div>
          <p className="eyebrow">EVALUATION CONTROL ROOM</p>
          <h1>让 Agent 的能力差异<br />变得可测量。</h1>
          <p>统一协议、冻结数据与独立任务在同一条生产评测链路中运行。</p>
        </div>
        <div className={`status-chip ${online ? "is-online" : "is-offline"}`}>
          <span className="status-dot" />
          {platform.isPending ? "正在连接" : online ? "服务在线" : "服务不可用"}
        </div>
      </header>

      <section className="dashboard-protocol" aria-label="Unified V3 协议状态">
        <article className="protocol-primary">
          <div className="panel-heading">
            <div><p className="eyebrow">PRIMARY PROTOCOL</p><h2>unified-v3</h2></div>
            <span className="panel-tag">UNIFIED V3 / ACTIVE</span>
          </div>
          <div className="protocol-volume"><strong>175</strong><span>QUESTIONS</span><p>中文 100 <i /> 英文 75</p></div>
          <div className="protocol-actions">
            <Link className="button" to="/runs?launch=1&profile=unified-v3">发起综合评测</Link>
            <Link className="button is-ghost" to="/leaderboard">查看排行榜</Link>
          </div>
        </article>
        <div className="protocol-facts">
          <article><span>冻结数据集</span><strong>13</strong><small>FROZEN DATASETS</small></article>
          <article><span>能力维度</span><strong>5</strong><small>CAPABILITIES</small></article>
          <article><span>单项权重</span><strong>20%</strong><small>EQUAL WEIGHT</small></article>
        </div>
      </section>

      <section className="dashboard-lower-grid">
        <article className="panel capability-panel">
          <div className="panel-heading">
            <div>
              <p className="eyebrow">SCORING PROFILE</p>
              <h2>能力权重档案</h2>
            </div>
            <span className="panel-tag">5 × 20%</span>
          </div>
          <div className="capability-list">
            {unifiedCapabilityRows.map((row) => (
              <div className="capability-row" key={row.label}><span className={`capability-mark ${row.tone}`} /><span>{row.label}</span><strong>{row.share}</strong></div>
            ))}
          </div>
        </article>

        <article className="panel profile-availability">
          <div className="panel-heading">
            <div><p className="eyebrow">PROFILE AVAILABILITY</p><h2>综合 + 五项专项</h2></div>
            <span className="availability-count">06</span>
          </div>
          <div className="availability-list"><strong>综合评测</strong>{unifiedCapabilityRows.map((row) => <span key={row.label}>{row.label}<i>READY</i></span>)}</div>
          <footer><span>API {platform.data?.version ?? "-"}</span><span>{platform.data?.environment ?? "等待连接"}</span><span>DeepSeek Chat</span></footer>
        </article>
      </section>
    </main>
  );
}

function AppNavigation({ operator, onLogout, loggingOut }: { operator: boolean; onLogout: () => void; loggingOut: boolean }) {
  return (
    <aside className="sidebar">
      <div className="brand">
        <span className="brand-glyph">AB</span>
        <div><strong>Agent Bench</strong><small>Evaluation control</small></div>
      </div>
      <nav className="primary-nav" aria-label="主导航">
        {operator && <NavLink to="/" end>总览</NavLink>}
        {operator && <NavLink to="/agents">Agent</NavLink>}
        {operator && <NavLink to="/runs">评测任务</NavLink>}
        <NavLink to="/leaderboard">排行榜</NavLink>
      </nav>
      <footer>
        <div className="session-role"><span>{operator ? "运营访问" : "只读访问"}</span><small>V0.1</small></div>
        <button type="button" className="logout-button" disabled={loggingOut} onClick={onLogout}>{loggingOut ? "退出中" : "退出"}</button>
      </footer>
    </aside>
  );
}

function AuthenticatedApp({ session }: { session: SessionInfo }) {
  const queryClient = useQueryClient();
  const logoutMutation = useMutation({
    mutationFn: logout,
    onSuccess: () => queryClient.setQueryData(["auth-session"], null)
  });
  const operator = session.role === "operator";

  return (
    <div className="app-shell">
      <AppNavigation operator={operator} loggingOut={logoutMutation.isPending} onLogout={() => logoutMutation.mutate()} />
      <div className="shell-content"><Routes>
        <Route path="/" element={operator ? <Dashboard /> : <Navigate to="/leaderboard" replace />} />
        <Route path="/agents" element={operator ? <AgentsPage /> : <Navigate to="/" replace />} />
        <Route path="/runs" element={operator ? <RunsPage /> : <Navigate to="/" replace />} />
        <Route path="/runs/:runId" element={operator ? <RunsPage /> : <Navigate to="/" replace />} />
        <Route path="/leaderboard" element={<LeaderboardPage />} />
        <Route path="/leaderboard/compare" element={<Suspense fallback={<main className="workspace"><div className="leaderboard-loading"><span className="status-dot" />正在加载对比视图</div></main>}><ComparePage /></Suspense>} />
      </Routes></div>
    </div>
  );
}

export function App() {
  const queryClient = useQueryClient();
  const sessionQuery = useQuery({
    queryKey: ["auth-session"],
    queryFn: fetchSession,
    retry: false,
    staleTime: 60_000
  });
  const loginMutation = useMutation({
    mutationFn: ({ role, password }: { role: SessionRole; password: string }) =>
      login(role, password),
    onSuccess: (session) => queryClient.setQueryData(["auth-session"], session)
  });

  if (sessionQuery.isPending) {
    return (
      <main className="auth-loading">
        <span className="status-dot" />
        正在确认登录状态
      </main>
    );
  }

  if (!sessionQuery.data) {
    return (
      <LoginPage
        pending={loginMutation.isPending}
        error={loginMutation.error instanceof Error ? loginMutation.error.message : undefined}
        onLogin={async (role, password) => {
          await loginMutation.mutateAsync({ role, password });
        }}
        onResetError={loginMutation.reset}
      />
    );
  }

  return <AuthenticatedApp session={sessionQuery.data} />;
}
