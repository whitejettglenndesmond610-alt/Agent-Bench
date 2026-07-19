import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { lazy, Suspense } from "react";
import { Link, Navigate, NavLink, Route, Routes, useLocation } from "react-router-dom";

import { fetchPlatformMeta, fetchSession, login, logout, SessionInfo, SessionRole, userLogin } from "./api";
import { AgentsPage } from "./AgentsPage";
import { LoadingState } from "./components";
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
const WorkspacePage = lazy(() =>
  import("./WorkspacePage").then((module) => ({ default: module.WorkspacePage }))
);
const WorkspaceComparePage = lazy(() =>
  import("./WorkspaceComparePage").then((module) => ({ default: module.WorkspaceComparePage }))
);
const EvaluationCasesPage = lazy(() =>
  import("./EvaluationCasesPage").then((module) => ({ default: module.EvaluationCasesPage }))
);
const DatasetReleasesPage = lazy(() =>
  import("./DatasetReleasesPage").then((module) => ({ default: module.DatasetReleasesPage }))
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
        <aside className={`dashboard-health ${online ? "is-online" : "is-offline"}`}>
          <div className="dashboard-health-heading">
            <div className={`status-chip ${online ? "is-online" : "is-offline"}`}>
              <span className="status-dot" />
              {platform.isPending ? "正在连接" : online ? "服务在线" : "服务不可用"}
            </div>
            <small>LIVE SYSTEM</small>
          </div>
          <strong>{online ? "生产评测链路运行正常" : "正在等待评测服务响应"}</strong>
          <p>{online ? "API、任务调度与结果发布服务保持连接。" : "页面会自动重新检查服务状态。"}</p>
          <dl>
            <div><dt>API 版本</dt><dd>{platform.data?.version ?? "--"}</dd></div>
            <div><dt>运行环境</dt><dd>{platform.data?.environment?.toUpperCase() ?? "--"}</dd></div>
            <div><dt>状态同步</dt><dd>30 SEC</dd></div>
          </dl>
        </aside>
      </header>

      <section className="dashboard-protocol" aria-label="Unified V3 协议状态">
        <article className="protocol-primary">
          <div className="panel-heading">
            <div><p className="eyebrow">PRIMARY PROTOCOL</p><h2>unified-v3</h2></div>
            <span className="panel-tag">UNIFIED V3 / ACTIVE</span>
          </div>
          <div className="protocol-volume"><strong>175</strong><span>QUESTIONS</span><p>中文 100 <i /> 英文 75</p><div className="protocol-language-bar" aria-label="中文 100 题，英文 75 题"><i /><i /></div></div>
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

type NavigationIconName = "overview" | "agents" | "runs" | "cases" | "datasets" | "workspace" | "leaderboard";

function NavigationIcon({ name }: { name: NavigationIconName }) {
  return (
    <svg className="nav-icon" viewBox="0 0 24 24" aria-hidden="true">
      {name === "overview" && <><path d="M4 13h6V4H4v9Z" /><path d="M14 20h6v-9h-6v9Z" /><path d="M4 20h6v-3H4v3Z" /><path d="M14 7h6V4h-6v3Z" /></>}
      {name === "agents" && <><circle cx="12" cy="8" r="3.5" /><path d="M5 20c.7-4 3-6 7-6s6.3 2 7 6" /><path d="M4 8H2.5M21.5 8H20M12 2.5V1" /></>}
      {name === "runs" && <><path d="M5 4h14v16H5z" /><path d="M8 9h8M8 13h5M8 17h7" /><path d="m15.5 4 1-2 1 2" /></>}
      {name === "cases" && <><path d="M7 3h10v4H7z" /><path d="M5 5H3v16h18V5h-2" /><path d="M8 12h8M8 16h5" /></>}
      {name === "datasets" && <><ellipse cx="12" cy="5" rx="7" ry="3" /><path d="M5 5v6c0 1.7 3.1 3 7 3s7-1.3 7-3V5" /><path d="M5 11v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6" /></>}
      {name === "workspace" && <><path d="M4 4h16v12H8l-4 4V4Z" /><path d="M8 9h8M8 12h5" /></>}
      {name === "leaderboard" && <><path d="M5 20v-7h4v7M10 20V8h4v12M15 20V4h4v16" /><path d="M3 20h18" /></>}
    </svg>
  );
}

function AppNavigation({ session, onLogout, loggingOut }: { session: SessionInfo; onLogout: () => void; loggingOut: boolean }) {
  const operator = session.role === "operator";
  const workspaceEnabled = session.workspace_enabled ?? operator;
  const displayName = session.display_name || (operator ? "运营访问" : "只读访问");
  return (
    <aside className="sidebar">
      <div className="brand">
        <span className="brand-glyph">AB</span>
        <div><strong>Agent Bench</strong><small>Evaluation control</small></div>
      </div>
      <nav className="primary-nav" aria-label="主导航">
        {operator && <NavLink to="/" end><NavigationIcon name="overview" /><span>总览</span></NavLink>}
        {operator && <NavLink to="/agents"><NavigationIcon name="agents" /><span>Agent</span></NavLink>}
        {operator && <NavLink to="/runs"><NavigationIcon name="runs" /><span>评测任务</span></NavLink>}
        {operator && <NavLink to="/evaluation-cases"><NavigationIcon name="cases" /><span>评测案例</span></NavLink>}
        {operator && <NavLink to="/dataset-releases"><NavigationIcon name="datasets" /><span>数据集发布</span></NavLink>}
        {workspaceEnabled && <NavLink to="/workspace"><NavigationIcon name="workspace" /><span>工作台</span></NavLink>}
        <NavLink to="/leaderboard"><NavigationIcon name="leaderboard" /><span>排行榜</span></NavLink>
      </nav>
      <footer>
        <div className="session-role">
          <span>{displayName}</span>
          <small>{(session.is_shared ?? true) ? "共享会话" : "个人账号"} · V0.1</small>
        </div>
        <button type="button" className="logout-button" disabled={loggingOut} onClick={onLogout}>{loggingOut ? "退出中" : "退出"}</button>
      </footer>
    </aside>
  );
}

const shellSections = [
  { prefix: "/agents", label: "Agent 接入", code: "INTEGRATIONS" },
  { prefix: "/runs", label: "评测任务", code: "EVALUATIONS" },
  { prefix: "/evaluation-cases", label: "评测案例", code: "CURATION" },
  { prefix: "/dataset-releases", label: "数据集发布", code: "DATA RELEASES" },
  { prefix: "/workspace", label: "Agent 工作台", code: "WORKSPACE" },
  { prefix: "/leaderboard", label: "排行榜", code: "LEADERBOARD" }
] as const;

function ShellTopbar({ session }: { session: SessionInfo }) {
  const location = useLocation();
  const section = shellSections.find((item) => location.pathname.startsWith(item.prefix))
    ?? { label: "总览", code: "OVERVIEW" };
  const displayName = session.display_name || (session.role === "operator" ? "运营访问" : "只读访问");
  const avatarLabel = displayName.trim().slice(0, 1).toUpperCase() || "A";

  return (
    <header className="shell-topbar">
      <div className="shell-breadcrumb">
        <span>AGENT BENCH</span>
        <i aria-hidden="true" />
        <strong>{section.label}</strong>
        <small>{section.code}</small>
      </div>
      <div className="shell-topbar-meta">
        <div className="shell-environment"><i /><span>系统在线</span><small>SYSTEM READY</small></div>
        <div className="shell-account">
          <span aria-hidden="true">{avatarLabel}</span>
          <div><strong>{session.role === "operator" ? "运营工作区" : "用户工作区"}</strong><small>{session.is_shared ?? true ? "SHARED SESSION" : "NAMED SESSION"}</small></div>
        </div>
      </div>
    </header>
  );
}

function AuthenticatedApp({ session }: { session: SessionInfo }) {
  const queryClient = useQueryClient();
  const logoutMutation = useMutation({
    mutationFn: logout,
    onSuccess: async () => {
      await queryClient.cancelQueries({ queryKey: ["workspace"] });
      queryClient.removeQueries({ queryKey: ["workspace"] });
      queryClient.setQueryData(["auth-session"], null);
    }
  });
  const operator = session.role === "operator";
  const workspaceEnabled = session.workspace_enabled ?? operator;
  const workspaceFallback = <main className="workspace"><LoadingState label="正在加载 Agent 工作台" /></main>;

  return (
    <div className="app-shell">
      <AppNavigation session={session} loggingOut={logoutMutation.isPending} onLogout={() => logoutMutation.mutate()} />
      <div className="shell-content">
        <ShellTopbar session={session} />
        <div className="shell-route"><Routes>
        <Route path="/" element={operator ? <Dashboard /> : <Navigate to="/leaderboard" replace />} />
        <Route path="/agents" element={operator ? <AgentsPage /> : <Navigate to="/" replace />} />
        <Route path="/runs" element={operator ? <RunsPage /> : <Navigate to="/" replace />} />
        <Route path="/runs/:runId" element={operator ? <RunsPage /> : <Navigate to="/" replace />} />
        <Route path="/evaluation-cases" element={operator ? <Suspense fallback={workspaceFallback}><EvaluationCasesPage userId={session.user_id} canPublish={operator && !session.is_shared} /></Suspense> : <Navigate to="/" replace />} />
        <Route path="/evaluation-cases/:caseId" element={operator ? <Suspense fallback={workspaceFallback}><EvaluationCasesPage userId={session.user_id} canPublish={operator && !session.is_shared} /></Suspense> : <Navigate to="/" replace />} />
        <Route path="/dataset-releases" element={operator ? <Suspense fallback={workspaceFallback}><DatasetReleasesPage userId={session.user_id} canPublish={operator && !session.is_shared} /></Suspense> : <Navigate to="/" replace />} />
        <Route path="/dataset-releases/:releaseId" element={operator ? <Suspense fallback={workspaceFallback}><DatasetReleasesPage userId={session.user_id} canPublish={operator && !session.is_shared} /></Suspense> : <Navigate to="/" replace />} />
        <Route path="/leaderboard" element={<LeaderboardPage />} />
        <Route path="/leaderboard/compare" element={<Suspense fallback={<main className="workspace"><LoadingState label="正在加载对比视图" /></main>}><ComparePage /></Suspense>} />
        <Route path="/workspace" element={workspaceEnabled ? <Suspense fallback={workspaceFallback}><WorkspacePage userId={session.user_id} canDraft={operator && !session.is_shared} /></Suspense> : <Navigate to="/" replace />} />
        <Route path="/workspace/compare" element={workspaceEnabled ? <Suspense fallback={workspaceFallback}><WorkspaceComparePage userId={session.user_id} canDraft={operator && !session.is_shared} /></Suspense> : <Navigate to="/" replace />} />
        <Route path="/workspace/compare/:comparisonId" element={workspaceEnabled ? <Suspense fallback={workspaceFallback}><WorkspaceComparePage userId={session.user_id} canDraft={operator && !session.is_shared} /></Suspense> : <Navigate to="/" replace />} />
        <Route path="/workspace/:conversationId" element={workspaceEnabled ? <Suspense fallback={workspaceFallback}><WorkspacePage userId={session.user_id} canDraft={operator && !session.is_shared} /></Suspense> : <Navigate to="/" replace />} />
        </Routes></div>
      </div>
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
    mutationFn: (input: { mode: "role"; role: SessionRole; password: string } | { mode: "account"; username: string; password: string }) =>
      input.mode === "role" ? login(input.role, input.password) : userLogin(input.username, input.password),
    onSuccess: (session) => {
      queryClient.removeQueries({ queryKey: ["workspace"] });
      queryClient.setQueryData(["auth-session"], session);
    }
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
          await loginMutation.mutateAsync({ mode: "role", role, password });
        }}
        onUserLogin={async (username, password) => {
          await loginMutation.mutateAsync({ mode: "account", username, password });
        }}
        onResetError={loginMutation.reset}
      />
    );
  }

  return <AuthenticatedApp session={sessionQuery.data} />;
}
