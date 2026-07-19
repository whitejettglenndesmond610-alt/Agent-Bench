import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { lazy, Suspense } from "react";
import { Navigate, NavLink, Route, Routes, useLocation, useParams } from "react-router-dom";

import { fetchSession, login, logout, SessionInfo, SessionRole, userLogin } from "./api";
import { AgentDetailPage } from "./AgentDetailPage";
import { AgentPlazaDetailPage, AgentPlazaPage } from "./AgentPlazaPage";
import { AgentsPage } from "./AgentsPage";
import { LoadingState } from "./components";
import { DashboardPage } from "./DashboardPage";
import { ExperimentsPage } from "./ExperimentsPage";
import { LoginPage } from "./LoginPage";
import { LeaderboardPage } from "./LeaderboardPage";
import { LeaderboardHistoryPage, LeaderboardSnapshotPage } from "./LeaderboardHistoryPage";
import { NotFoundPage } from "./NotFoundPage";
import { PromptTemplatesPage } from "./PromptTemplatesPage";
import { RecordsPage } from "./RecordsPage";
import { RunEvidencePage } from "./RunEvidencePage";
import { RunsPage } from "./RunsPage";
import { ExperiencePage } from "./ExperiencePage";
import { UserHomePage } from "./UserHomePage";

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

type NavigationIconName = "overview" | "agents" | "runs" | "experiments" | "cases" | "datasets" | "workspace" | "leaderboard" | "home" | "plaza" | "compare" | "records" | "templates" | "experience";

function NavigationIcon({ name }: { name: NavigationIconName }) {
  return (
    <svg className="nav-icon" viewBox="0 0 24 24" aria-hidden="true">
      {name === "overview" && <><path d="M4 13h6V4H4v9Z" /><path d="M14 20h6v-9h-6v9Z" /><path d="M4 20h6v-3H4v3Z" /><path d="M14 7h6V4h-6v3Z" /></>}
      {name === "agents" && <><circle cx="12" cy="8" r="3.5" /><path d="M5 20c.7-4 3-6 7-6s6.3 2 7 6" /><path d="M4 8H2.5M21.5 8H20M12 2.5V1" /></>}
      {name === "runs" && <><path d="M5 4h14v16H5z" /><path d="M8 9h8M8 13h5M8 17h7" /><path d="m15.5 4 1-2 1 2" /></>}
      {name === "experiments" && <><path d="M9 3v5l-5 9a2 2 0 0 0 2 3h12a2 2 0 0 0 2-3l-5-9V3" /><path d="M8 3h8M7 15h10" /><circle cx="10" cy="17" r=".5" /></>}
      {name === "cases" && <><path d="M7 3h10v4H7z" /><path d="M5 5H3v16h18V5h-2" /><path d="M8 12h8M8 16h5" /></>}
      {name === "datasets" && <><ellipse cx="12" cy="5" rx="7" ry="3" /><path d="M5 5v6c0 1.7 3.1 3 7 3s7-1.3 7-3V5" /><path d="M5 11v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6" /></>}
      {name === "workspace" && <><path d="M4 4h16v12H8l-4 4V4Z" /><path d="M8 9h8M8 12h5" /></>}
      {name === "leaderboard" && <><path d="M5 20v-7h4v7M10 20V8h4v12M15 20V4h4v16" /><path d="M3 20h18" /></>}
      {name === "home" && <><path d="m3 11 9-8 9 8" /><path d="M5 10v10h14V10M9 20v-6h6v6" /></>}
      {name === "plaza" && <><circle cx="12" cy="12" r="9" /><path d="M3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18" /></>}
      {name === "compare" && <><path d="M4 5h7v6H4zM13 13h7v6h-7z" /><path d="m14 5 2-2 2 2M17 3v7M10 19l-2 2-2-2M7 21v-7" /></>}
      {name === "records" && <><path d="M5 3h14v18H5zM8 8h8M8 12h8M8 16h5" /></>}
      {name === "templates" && <><path d="M7 3h10l3 3v15H7z" /><path d="M7 6H4v15h3M10 10h7M10 14h7" /></>}
      {name === "experience" && <><path d="M9 3h6v5l5 9a2 2 0 0 1-2 3H6a2 2 0 0 1-2-3l5-9V3Z" /><path d="M7 15h10" /></>}
    </svg>
  );
}

function AppNavigation({ session, onLogout, loggingOut }: { session: SessionInfo; onLogout: () => void; loggingOut: boolean }) {
  const operator = session.role === "operator";
  const workspaceEnabled = session.workspace_enabled ?? operator;
  const displayName = session.display_name || (operator ? "评测管理员" : "普通用户");
  return (
    <aside className="sidebar">
      <div className="brand">
        <span className="brand-glyph">AB</span>
        <div><strong>Agent Bench</strong><small>Agent evaluation</small></div>
      </div>
      <nav className="primary-nav" aria-label="主导航">
        {operator && <span className="nav-section-label">管理台</span>}
        {operator && <NavLink to="/" end><NavigationIcon name="overview" /><span>评测总览</span></NavLink>}
        {operator && <NavLink to="/agents"><NavigationIcon name="agents" /><span>Agent 中心</span></NavLink>}
        {operator && <NavLink to="/experiments"><NavigationIcon name="experiments" /><span>评测实验</span></NavLink>}
        {operator && <NavLink to="/runs"><NavigationIcon name="runs" /><span>评测运行</span></NavLink>}
        {operator && <span className="nav-section-label">数据与策展</span>}
        {operator && <NavLink to="/evaluation-cases"><NavigationIcon name="cases" /><span>案例策展</span></NavLink>}
        {operator && <NavLink to="/dataset-releases"><NavigationIcon name="datasets" /><span>数据集发布</span></NavLink>}
        {operator && workspaceEnabled && <span className="nav-section-label">用户体验</span>}
        {workspaceEnabled && <NavLink to="/home"><NavigationIcon name="home" /><span>首页</span></NavLink>}
        {workspaceEnabled && <NavLink to="/plaza"><NavigationIcon name="plaza" /><span>Agent 广场</span></NavLink>}
        {workspaceEnabled && <NavLink to="/workspace"><NavigationIcon name="workspace" /><span>在线对话</span></NavLink>}
        {workspaceEnabled && <NavLink to="/compare"><NavigationIcon name="compare" /><span>多 Agent 对比</span></NavLink>}
        <NavLink to="/leaderboard"><NavigationIcon name="leaderboard" /><span>排行榜</span></NavLink>
        {workspaceEnabled && <NavLink to="/records"><NavigationIcon name="records" /><span>我的记录</span></NavLink>}
        {workspaceEnabled && <NavLink to="/templates"><NavigationIcon name="templates" /><span>Prompt 模板</span></NavLink>}
        {workspaceEnabled && <NavLink to="/experience"><NavigationIcon name="experience" /><span>体验任务</span></NavLink>}
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
  { prefix: "/agents", label: "Agent 中心", code: "AGENT CENTER" },
  { prefix: "/experiments", label: "评测实验", code: "EXPERIMENTS" },
  { prefix: "/runs", label: "评测运行", code: "EVALUATION RUNS" },
  { prefix: "/evaluation-cases", label: "案例策展", code: "CURATION" },
  { prefix: "/dataset-releases", label: "数据集发布", code: "DATA RELEASES" },
  { prefix: "/workspace", label: "Agent 在线运行", code: "PLAYGROUND" },
  { prefix: "/home", label: "首页", code: "USER HOME" },
  { prefix: "/plaza", label: "Agent 广场", code: "AGENT PLAZA" },
  { prefix: "/compare", label: "多 Agent 对比", code: "LIVE COMPARE" },
  { prefix: "/records", label: "我的记录", code: "MY RECORDS" },
  { prefix: "/templates", label: "Prompt 模板", code: "PROMPT LIBRARY" },
  { prefix: "/experience", label: "体验任务", code: "PRACTICE LAB" },
  { prefix: "/leaderboard", label: "排行榜", code: "LEADERBOARD" }
] as const;

function ShellTopbar({ session }: { session: SessionInfo }) {
  const location = useLocation();
  const section = shellSections.find((item) => location.pathname.startsWith(item.prefix))
    ?? { label: "评测总览", code: "OVERVIEW" };
  const displayName = session.display_name || (session.role === "operator" ? "评测管理员" : "普通用户");
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
        <div className="shell-environment"><span>Agent 评测</span><small>{session.role === "operator" ? "OPERATOR" : "USER"}</small></div>
        <div className="shell-account">
          <span aria-hidden="true">{avatarLabel}</span>
          <div><strong>{session.role === "operator" ? "评测管理台" : "用户工作区"}</strong><small>{session.is_shared ?? true ? "SHARED SESSION" : "NAMED SESSION"}</small></div>
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
      await queryClient.cancelQueries();
      queryClient.removeQueries({ predicate: (query) => query.queryKey[0] !== "auth-session" });
      queryClient.setQueryData(["auth-session"], null);
    }
  });
  const operator = session.role === "operator";
  const workspaceEnabled = session.workspace_enabled ?? operator;
  const workspaceFallback = <main className="workspace"><LoadingState label="正在加载 Agent 在线运行" /></main>;

  return (
    <div className="app-shell">
      <AppNavigation session={session} loggingOut={logoutMutation.isPending} onLogout={() => logoutMutation.mutate()} />
      <div className="shell-content">
        <ShellTopbar session={session} />
        <div className="shell-route"><Routes>
        <Route path="/" element={operator ? <DashboardPage /> : <Navigate to={workspaceEnabled ? "/home" : "/leaderboard"} replace />} />
        <Route path="/agents" element={operator ? <AgentsPage /> : <Navigate to="/" replace />} />
        <Route path="/agents/:agentId" element={operator ? <AgentDetailPage /> : <Navigate to="/" replace />} />
        <Route path="/experiments" element={operator ? <ExperimentsPage /> : <Navigate to="/" replace />} />
        <Route path="/experiments/:experimentId" element={operator ? <ExperimentsPage /> : <Navigate to="/" replace />} />
        <Route path="/runs" element={operator ? <RunsPage /> : <Navigate to="/" replace />} />
        <Route path="/runs/:runId" element={operator ? <RunsPage /> : <Navigate to="/" replace />} />
        <Route path="/runs/:runId/evidence" element={operator ? <RunEvidencePage /> : <Navigate to="/" replace />} />
        <Route path="/runs/:runId/evidence/:executionItemId" element={operator ? <RunEvidencePage /> : <Navigate to="/" replace />} />
        <Route path="/evaluation-cases" element={operator ? <Suspense fallback={workspaceFallback}><EvaluationCasesPage userId={session.user_id} canPublish={operator && !session.is_shared} /></Suspense> : <Navigate to="/" replace />} />
        <Route path="/evaluation-cases/:caseId" element={operator ? <Suspense fallback={workspaceFallback}><EvaluationCasesPage userId={session.user_id} canPublish={operator && !session.is_shared} /></Suspense> : <Navigate to="/" replace />} />
        <Route path="/dataset-releases" element={operator ? <Suspense fallback={workspaceFallback}><DatasetReleasesPage userId={session.user_id} canPublish={operator && !session.is_shared} /></Suspense> : <Navigate to="/" replace />} />
        <Route path="/dataset-releases/:releaseId" element={operator ? <Suspense fallback={workspaceFallback}><DatasetReleasesPage userId={session.user_id} canPublish={operator && !session.is_shared} /></Suspense> : <Navigate to="/" replace />} />
        <Route path="/leaderboard" element={<LeaderboardPage isOperator={operator} workspaceEnabled={workspaceEnabled} />} />
        <Route path="/leaderboard/compare" element={<Suspense fallback={<main className="workspace"><LoadingState label="正在加载对比视图" /></main>}><ComparePage /></Suspense>} />
        <Route path="/leaderboard/history" element={<LeaderboardHistoryPage />} />
        <Route path="/leaderboard/history/:snapshotId" element={<LeaderboardSnapshotPage />} />
        <Route path="/home" element={workspaceEnabled ? <UserHomePage session={session} /> : <Navigate to="/leaderboard" replace />} />
        <Route path="/plaza" element={workspaceEnabled ? <AgentPlazaPage userId={session.user_id} /> : <Navigate to="/leaderboard" replace />} />
        <Route path="/plaza/:agentId" element={workspaceEnabled ? <AgentPlazaDetailPage userId={session.user_id} /> : <Navigate to="/leaderboard" replace />} />
        <Route path="/records" element={workspaceEnabled ? <RecordsPage userId={session.user_id} /> : <Navigate to="/leaderboard" replace />} />
        <Route path="/templates" element={workspaceEnabled ? <PromptTemplatesPage userId={session.user_id} /> : <Navigate to="/leaderboard" replace />} />
        <Route path="/experience" element={workspaceEnabled ? <ExperiencePage userId={session.user_id} /> : <Navigate to="/leaderboard" replace />} />
        <Route path="/experience/:taskSlug" element={workspaceEnabled ? <ExperiencePage userId={session.user_id} /> : <Navigate to="/leaderboard" replace />} />
        <Route path="/workspace" element={workspaceEnabled ? <Suspense fallback={workspaceFallback}><WorkspacePage userId={session.user_id} canDraft={operator && !session.is_shared} /></Suspense> : <Navigate to="/" replace />} />
        <Route path="/compare" element={workspaceEnabled ? <Suspense fallback={workspaceFallback}><WorkspaceComparePage userId={session.user_id} canDraft={operator && !session.is_shared} /></Suspense> : <Navigate to="/" replace />} />
        <Route path="/compare/:comparisonId" element={workspaceEnabled ? <Suspense fallback={workspaceFallback}><WorkspaceComparePage userId={session.user_id} canDraft={operator && !session.is_shared} /></Suspense> : <Navigate to="/" replace />} />
        <Route path="/workspace/compare" element={<LegacyCompareRedirect />} />
        <Route path="/workspace/compare/:comparisonId" element={<LegacyCompareRedirect />} />
        <Route path="/workspace/:conversationId" element={workspaceEnabled ? <Suspense fallback={workspaceFallback}><WorkspacePage userId={session.user_id} canDraft={operator && !session.is_shared} /></Suspense> : <Navigate to="/" replace />} />
        <Route path="*" element={<NotFoundPage operator={operator} />} />
        </Routes></div>
      </div>
    </div>
  );
}

function LegacyCompareRedirect() {
  const { comparisonId } = useParams();
  const location = useLocation();
  return <Navigate to={`/compare${comparisonId ? `/${comparisonId}` : ""}${location.search}`} replace />;
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
      queryClient.removeQueries({ predicate: (query) => query.queryKey[0] !== "auth-session" });
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
