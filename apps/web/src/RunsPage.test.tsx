import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";

import { RunDatasetOption, RunOptions, RunProgress } from "./api";
import { RunsPage } from "./RunsPage";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  document.cookie = "agent_eval_csrf=; Max-Age=0";
});

function response(body: object, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 400 ? "Request failed" : "OK",
    json: async () => body
  };
}

const reasoningDataset: RunDatasetOption = {
  benchmark_id: "benchmark-1",
  dataset_version_id: "dataset-1",
  name: "C-Eval 专项集",
  ability: "reasoning",
  language: "zh",
  version: 3,
  question_count: 40,
  cohort_key: "unified-v3"
};

const retrievalDataset: RunDatasetOption = {
  benchmark_id: "benchmark-2",
  dataset_version_id: "dataset-2",
  name: "BrowseComp",
  ability: "retrieval",
  language: "en",
  version: 2,
  question_count: 30,
  cohort_key: "unified-v3"
};

function runOptions(): RunOptions {
  const compatibility = ["unified-v3", "unified-v3-reasoning"].map((profileKey) => ({
    profile_key: profileKey,
    compatible: true,
    oversized_question_count: 0,
    max_question_characters: 120,
    citation_required_count: profileKey === "unified-v3" ? 30 : 0,
    blocking_reasons: [],
    warnings: []
  }));
  return {
    agents: [
      { agent_id: "agent-1", agent_version_id: "version-1", name: "Agent Alpha", provider: "Provider A", version: 1, lifecycle_status: "active", max_prompt_characters: null, supports_citations: true, profile_compatibility: compatibility },
      { agent_id: "agent-2", agent_version_id: "version-2", name: "Agent Beta", provider: "Provider B", version: 2, lifecycle_status: "active", max_prompt_characters: 4000, supports_citations: false, profile_compatibility: compatibility }
    ],
    datasets: [reasoningDataset, retrievalDataset],
    dataset_version_ids: ["dataset-1", "dataset-2"],
    question_count: 70,
    profiles: [
      {
        key: "unified-v3",
        name: "综合基准",
        description: "覆盖全部能力与正式榜单。",
        scope: "comprehensive",
        target_ability: null,
        dataset_version_ids: ["dataset-1", "dataset-2"],
        question_count: 70,
        benchmark_count: 2,
        publishes_to: ["leaderboard"],
        ranking_eligible: true,
        score_config_id: "score-1",
        ability_weights: { reasoning: 20, retrieval: 20, data_reasoning: 20, instruction_following: 20, long_context: 20 },
        datasets: [reasoningDataset, retrievalDataset]
      },
      {
        key: "unified-v3-reasoning",
        name: "推理专项",
        description: "只运行推理能力题集。",
        scope: "specialist",
        target_ability: "reasoning",
        dataset_version_ids: ["dataset-1"],
        question_count: 40,
        benchmark_count: 1,
        publishes_to: ["specialist-leaderboard"],
        ranking_eligible: true,
        score_config_id: "score-1",
        ability_weights: { reasoning: 20, retrieval: 20, data_reasoning: 20, instruction_following: 20, long_context: 20 },
        datasets: [reasoningDataset]
      }
    ],
    default_profile_key: "unified-v3",
    judge_config_id: "judge-1",
    judge_model: "deepseek-chat",
    score_config_id: "score-1",
    ability_weights: { reasoning: 20, retrieval: 20, data_reasoning: 20, instruction_following: 20, long_context: 20 },
    ready: true,
    issues: []
  };
}

function progress(
  desiredState: "running" | "canceled" = "running",
  name = "独立评测"
): RunProgress {
  return {
    id: "run-1",
    name,
    status: "running",
    desired_state: desiredState,
    stage: "inference",
    profile_key: "unified-v3-reasoning",
    run_scope: "specialist",
    target_ability: "reasoning",
    ranking_eligible: true,
    trial_count: 5,
    heartbeat_at: "2026-07-16T08:02:00Z",
    started_at: "2026-07-16T08:00:00Z",
    finished_at: null,
    system_error: null,
    created_at: "2026-07-16T08:00:00Z",
    total: 40,
    completed: 14,
    failed: 1,
    retried: 2,
    processed: 15,
    progress_percent: 37.5,
    agents: [
      {
        run_agent_id: "ra-1",
        agent_id: "agent-1",
        agent_version_id: "version-1",
        agent_name: "Agent Alpha",
        provider: "Provider A",
        version: 1,
        total: 40,
        completed: 14,
        failed: 1,
        retried: 2,
        processed: 15,
        failure_categories: { agent_input_limit: 1 },
        validity_status: "valid",
        validity_reason: null,
        configuration_failure_count: 0,
        configuration_failure_rate: 0,
        result: {
          overall_score: 72.5,
          ability_scores: { reasoning: 80, instruction_following: 77 },
          answer_quality: 75,
          citation_quality: 60,
          success_rate: 98,
          anomaly_rate: 2,
          http_call_count: 42,
          validity_status: "valid",
          validity_reason: null
        }
      }
    ]
  };
}

function experimentDetail() {
  return {
    id: "experiment-1",
    name: "专项独立评测",
    creator: { user_id: null, subject_key: "operator", display_name: "Shared operator" },
    status: "queued",
    protocol_hash: "protocol-1",
    profile_key: "unified-v3-reasoning",
    run_scope: "specialist",
    target_ability: "reasoning",
    member_count: 2,
    run_counts: { draft: 0, queued: 2, running: 0, paused: 0, scoring: 0, completed: 0, published: 0, system_failed: 0, canceled: 0 },
    total: 80,
    processed: 0,
    completed: 0,
    failed: 0,
    progress_percent: 0,
    created_at: "2026-07-16T08:00:00Z",
    updated_at: "2026-07-16T08:00:00Z",
    members: [],
    comparison_metrics: []
  };
}

function renderPage(initialEntry = "/runs") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } }
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/runs" element={<RunsPage />} />
          <Route path="/runs/:runId" element={<RunsPage />} />
          <Route path="/experiments/:experimentId" element={<h1>专项独立评测</h1>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

async function reachReview(agentNames: string[], profileName = "推理专项 Profile") {
  if (!screen.queryByRole("dialog")) {
    fireEvent.click(screen.getByRole("button", { name: "+ 发起评测" }));
  }
  await screen.findByRole("checkbox", { name: /Agent Alpha/ });
  fireEvent.change(screen.getByLabelText(/^任务名称/), { target: { value: "专项独立评测" } });
  for (const agentName of agentNames) {
    fireEvent.click(screen.getByRole("checkbox", { name: new RegExp(agentName) }));
  }
  fireEvent.click(screen.getByRole("button", { name: "下一步：选择 Profile" }));
  fireEvent.click(screen.getByRole("radio", { name: profileName }));
  fireEvent.click(screen.getByRole("button", { name: "下一步：确认配置" }));
}

test("submits the chosen per-question trial count with a single-agent run", async () => {
  let createBody: Record<string, unknown> | null = null;
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/evaluation-runs" && !init?.method) return Promise.resolve(response([]));
    if (input === "/api/v1/evaluation-runs/options") return Promise.resolve(response(runOptions()));
    if (input === "/api/v1/evaluation-runs" && init?.method === "POST") {
      createBody = JSON.parse(String(init.body));
      return Promise.resolve(response({ id: "run-1", name: "专项独立评测", status: "queued", trial_count: 3, question_count: 40, execution_item_count: 120 }, 201));
    }
    if (input === "/api/v1/evaluation-runs/run-1/progress") {
      return Promise.resolve(response(progress("running", "专项独立评测")));
    }
    throw new Error(`Unexpected request: ${input}`);
  }));
  renderPage();

  await screen.findByRole("heading", { name: "尚未发起评测" });
  fireEvent.click(screen.getByRole("button", { name: "+ 发起评测" }));
  fireEvent.click(await screen.findByRole("checkbox", { name: /Agent Alpha/ }));
  fireEvent.click(screen.getByRole("button", { name: "下一步：选择 Profile" }));
  fireEvent.change(screen.getByLabelText(/^每题运行次数/), { target: { value: "3" } });
  fireEvent.click(screen.getByRole("button", { name: "下一步：确认配置" }));

  expect(screen.getByText("每题运行").parentElement).toHaveTextContent("3 次");
  fireEvent.click(screen.getByRole("button", { name: "创建单 Agent 评测运行" }));

  expect(await screen.findByRole("heading", { name: "专项独立评测" })).toBeInTheDocument();
  expect(createBody).toMatchObject({ trial_count: 3, agent_version_ids: ["version-1"] });
});

test("omits trial_count from the payload when the selector stays on default", async () => {
  let createBody: Record<string, unknown> | null = null;
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/evaluation-runs" && !init?.method) return Promise.resolve(response([]));
    if (input === "/api/v1/evaluation-runs/options") return Promise.resolve(response(runOptions()));
    if (input === "/api/v1/evaluation-runs" && init?.method === "POST") {
      createBody = JSON.parse(String(init.body));
      return Promise.resolve(response({ id: "run-1", name: "专项独立评测", status: "queued", trial_count: 5, question_count: 40, execution_item_count: 200 }, 201));
    }
    if (input === "/api/v1/evaluation-runs/run-1/progress") {
      return Promise.resolve(response(progress("running", "专项独立评测")));
    }
    throw new Error(`Unexpected request: ${input}`);
  }));
  renderPage();

  await screen.findByRole("heading", { name: "尚未发起评测" });
  await reachReview(["Agent Alpha"]);

  expect(screen.getByText("每题运行").parentElement).toHaveTextContent("默认 5 次");
  fireEvent.click(screen.getByRole("button", { name: "创建单 Agent 评测运行" }));

  expect(await screen.findByRole("heading", { name: "专项独立评测" })).toBeInTheDocument();
  expect(createBody).not.toHaveProperty("trial_count");
});

test("shows trial count and reliability metrics on the run detail when present", async () => {
  const withReliability = progress();
  withReliability.agents[0].result = {
    ...withReliability.agents[0].result!,
    pass_at_1: 96,
    pass_at_k: 100,
    pass_pow_k: 80,
    ci_low: 70,
    ci_high: 75
  };
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string) => {
    if (input === "/api/v1/evaluation-runs/run-1/progress") return Promise.resolve(response(withReliability));
    if (input === "/api/v1/evaluation-runs") return Promise.resolve(response([]));
    throw new Error(`Unexpected request: ${input}`);
  }));
  renderPage("/runs/run-1");

  expect(await screen.findByText("每题 5 次")).toBeInTheDocument();
  expect(screen.getByText("pass@1 96.00%")).toBeInTheDocument();
  expect(screen.getByText("pass^k 80.00%")).toBeInTheDocument();
  expect(screen.getByText("95% CI 70.00–75.00")).toBeInTheDocument();
});

test("shows cost metrics on the run detail when the result summary has them", async () => {
  const withCost = progress();
  withCost.agents[0].result = {
    ...withCost.agents[0].result!,
    total_cost_usd: 0.0027,
    cost_per_task_usd: 0.00045
  };
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string) => {
    if (input === "/api/v1/evaluation-runs/run-1/progress") return Promise.resolve(response(withCost));
    if (input === "/api/v1/evaluation-runs") return Promise.resolve(response([]));
    throw new Error(`Unexpected request: ${input}`);
  }));
  renderPage("/runs/run-1");

  expect(await screen.findByText("成本 $0.0027")).toBeInTheDocument();
  expect(screen.getByText("$/题 $0.00045")).toBeInTheDocument();
});

test("shows a cost placeholder when the result summary has no pricing", async () => {
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string) => {
    if (input === "/api/v1/evaluation-runs/run-1/progress") return Promise.resolve(response(progress()));
    if (input === "/api/v1/evaluation-runs") return Promise.resolve(response([]));
    throw new Error(`Unexpected request: ${input}`);
  }));
  renderPage("/runs/run-1");

  expect(await screen.findByText("成本 —")).toBeInTheDocument();
  expect(screen.getByText("$/题 —")).toBeInTheDocument();
});

test("hides reliability metrics when the result summary has none", async () => {  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string) => {
    if (input === "/api/v1/evaluation-runs/run-1/progress") return Promise.resolve(response(progress()));
    if (input === "/api/v1/evaluation-runs") return Promise.resolve(response([]));
    throw new Error(`Unexpected request: ${input}`);
  }));
  const page = renderPage("/runs/run-1");

  expect(await screen.findByText("每题 5 次")).toBeInTheDocument();
  expect(screen.queryByText(/pass@1/)).not.toBeInTheDocument();
  expect(screen.queryByText(/pass\^k/)).not.toBeInTheDocument();
  expect(page.container.textContent).not.toContain("null");
});

test("creates one persisted experiment for multiple selected Agents with a stable idempotency key", async () => {
  vi.spyOn(crypto, "randomUUID").mockReturnValue("00000000-0000-4000-8000-000000000301");
  let createBody: Record<string, unknown> | null = null;
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/evaluation-runs" && !init?.method) return Promise.resolve(response([]));
    if (input === "/api/v1/evaluation-runs/options") return Promise.resolve(response(runOptions()));
    if (input === "/api/v1/evaluation-experiments" && init?.method === "POST") {
      createBody = JSON.parse(String(init.body));
      expect(init.headers).toMatchObject({ "X-CSRF-Token": "csrf-token" });
      return Promise.resolve(response(experimentDetail(), 201));
    }
    if (input === "/api/v1/evaluation-experiments/experiment-1") return Promise.resolve(response(experimentDetail()));
    throw new Error(`Unexpected request: ${input}`);
  }));
  document.cookie = "agent_eval_csrf=csrf-token";
  renderPage();

  expect(await screen.findByRole("heading", { name: "尚未发起评测" })).toBeInTheDocument();
  await reachReview(["Agent Alpha", "Agent Beta"]);

  expect(screen.getByText("实验成员").parentElement).toHaveTextContent("2 个");
  expect(screen.getByText("每任务题目").parentElement).toHaveTextContent("40");
  expect(screen.getByText("预计总调用").parentElement).toHaveTextContent("80");
  expect(screen.getByText("C-Eval 专项集")).toBeInTheDocument();
  expect(screen.getByText("specialist-leaderboard")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "创建 2 Agent 评测实验" }));

  expect(await screen.findByRole("heading", { name: "专项独立评测" })).toBeInTheDocument();
  expect(createBody).toEqual({
    name: "专项独立评测",
    idempotency_key: "00000000-0000-4000-8000-000000000301",
    agent_version_ids: ["version-1", "version-2"],
    profile_key: "unified-v3-reasoning",
    judge_config_id: "judge-1",
    score_config_id: "score-1"
  });
});

test("keeps a one-Agent launch on the single-run endpoint and navigates to run detail", async () => {
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/evaluation-runs" && !init?.method) return Promise.resolve(response([]));
    if (input === "/api/v1/evaluation-runs/options") return Promise.resolve(response(runOptions()));
    if (input === "/api/v1/evaluation-runs" && init?.method === "POST") {
      return Promise.resolve(response({ id: "run-1", name: "专项独立评测", status: "queued", question_count: 40, execution_item_count: 40 }, 201));
    }
    if (input === "/api/v1/evaluation-runs/run-1/progress") {
      return Promise.resolve(response(progress("running", "专项独立评测")));
    }
    throw new Error(`Unexpected request: ${input}`);
  }));
  renderPage();

  await screen.findByRole("heading", { name: "尚未发起评测" });
  await reachReview(["Agent Alpha"]);
  fireEvent.click(screen.getByRole("button", { name: "创建单 Agent 评测运行" }));

  expect(await screen.findByRole("heading", { name: "专项独立评测" })).toBeInTheDocument();
  expect(screen.getByText("37.50%")).toBeInTheDocument();
  expect(screen.getByText("输入超限")).toBeInTheDocument();
  expect(screen.getByText("72.50")).toBeInTheDocument();
  expect(screen.getByText("指令遵循")).toBeInTheDocument();
  expect(screen.getByText("长上下文")).toBeInTheDocument();
  expect(screen.getAllByText("—").length).toBeGreaterThan(0);
});

test("requires explicit confirmation before forcing a duplicate result retest", async () => {
  const requestBodies: Array<Record<string, unknown>> = [];
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/evaluation-runs" && !init?.method) return Promise.resolve(response([]));
    if (input === "/api/v1/evaluation-runs/options") return Promise.resolve(response(runOptions()));
    if (input === "/api/v1/evaluation-runs" && init?.method === "POST") {
      const body = JSON.parse(String(init.body));
      requestBodies.push(body);
      if (!body.force_retest) {
        return Promise.resolve(response({ detail: { code: "evaluation_result_exists", message: "result exists" } }, 409));
      }
       return Promise.resolve(response({ id: "run-1", name: "专项独立评测", status: "queued", question_count: 40, execution_item_count: 40 }, 201));
    }
    if (input === "/api/v1/evaluation-runs/run-1/progress") return Promise.resolve(response(progress()));
    throw new Error(`Unexpected request: ${input}`);
  }));
  renderPage();

  await screen.findByRole("heading", { name: "尚未发起评测" });
  await reachReview(["Agent Alpha"]);
  fireEvent.click(screen.getByRole("button", { name: "创建单 Agent 评测运行" }));

  expect(await screen.findByText("已有可用评测结果")).toBeInTheDocument();
  expect(requestBodies).toHaveLength(1);
  expect(requestBodies[0]).not.toHaveProperty("force_retest");
  fireEvent.click(screen.getByRole("button", { name: "确认重新评测" }));

  expect(await screen.findByRole("heading", { name: "独立评测" })).toBeInTheDocument();
  expect(requestBodies).toHaveLength(2);
  expect(requestBodies[1]).toMatchObject({ force_retest: true });
});

test("safely preselects Agent and Profile from quick-launch query parameters", async () => {
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string) => {
    if (input === "/api/v1/evaluation-runs") return Promise.resolve(response([]));
    if (input === "/api/v1/evaluation-runs/options") return Promise.resolve(response(runOptions()));
    throw new Error(`Unexpected request: ${input}`);
  }));
  renderPage("/runs?launch=1&agentVersion=version-2&profile=unified-v3-reasoning");

  expect(await screen.findByRole("dialog")).toBeInTheDocument();
  expect(await screen.findByRole("checkbox", { name: /Agent Beta/ })).toBeChecked();
  expect(screen.getByRole("checkbox", { name: /Agent Alpha/ })).not.toBeChecked();
  fireEvent.click(screen.getByRole("button", { name: "下一步：选择 Profile" }));
  expect(screen.getByRole("radio", { name: "推理专项 Profile" })).toBeChecked();
});

test("does not surface or block on an Agent prompt limit", async () => {
  const options = runOptions();
  options.agents[0].profile_compatibility[0] = {
    ...options.agents[0].profile_compatibility[0],
    compatible: true,
    oversized_question_count: 12,
    blocking_reasons: []
  };
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string) => {
    if (input === "/api/v1/evaluation-runs") return Promise.resolve(response([]));
    if (input === "/api/v1/evaluation-runs/options") return Promise.resolve(response(options));
    throw new Error(`Unexpected request: ${input}`);
  }));
  renderPage();

  await screen.findByRole("heading", { name: "尚未发起评测" });
  fireEvent.click(screen.getByRole("button", { name: "+ 发起评测" }));
  fireEvent.click(await screen.findByRole("checkbox", { name: /Agent Alpha/ }));
  fireEvent.click(screen.getByRole("button", { name: "下一步：选择 Profile" }));

  expect(screen.getByRole("radio", { name: "综合基准 Profile" })).toBeEnabled();
  expect(screen.queryByText(/4000 字符限制/)).not.toBeInTheDocument();
});

test("shows an invalid configuration result without publishing it", async () => {
  const invalidProgress = progress();
  invalidProgress.agents[0].validity_status = "invalid";
  invalidProgress.agents[0].validity_reason = "configuration_failure_rate_at_least_20_percent";
  invalidProgress.agents[0].configuration_failure_count = 12;
  invalidProgress.agents[0].configuration_failure_rate = 30;
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string) => {
    if (input === "/api/v1/evaluation-runs/run-1/progress") return Promise.resolve(response(invalidProgress));
    if (input === "/api/v1/evaluation-runs") return Promise.resolve(response([]));
    throw new Error(`Unexpected request: ${input}`);
  }));
  renderPage("/runs/run-1");

  expect(await screen.findByText("接入配置无效，不进入排行榜")).toBeInTheDocument();
  expect(screen.getByText(/12 项 \/ 30.00%/)).toBeInTheDocument();
});

test("confirms cancellation and refreshes the persisted desired state", async () => {
  let cancelRequested = false;
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/evaluation-runs/run-1/progress") {
      return Promise.resolve(response(progress(cancelRequested ? "canceled" : "running")));
    }
    if (input === "/api/v1/evaluation-runs/run-1/cancel" && init?.method === "POST") {
      cancelRequested = true;
      return Promise.resolve(response(progress("canceled")));
    }
    if (input === "/api/v1/evaluation-runs" && !init?.method) return Promise.resolve(response([]));
    throw new Error(`Unexpected request: ${input}`);
  }));
  renderPage("/runs/run-1");

  expect(await screen.findByText("37.50%")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "取消任务" }));
  expect(screen.getByRole("heading", { name: "确认取消评测？" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "确认取消任务" }));

  await waitFor(() => expect(screen.getByText("取消处理中")).toBeInTheDocument());
  expect(cancelRequested).toBe(true);
});

test("requeues only the unfinished scoring work after a Judge system failure", async () => {
  let retried = false;
  const failedProgress = {
    ...progress(),
    status: "system_failed",
    stage: "judging",
    system_error: "answer_scoring_judge_rate_limit",
    finished_at: "2026-07-16T08:10:00Z"
  };
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (input === "/api/v1/evaluation-runs/run-1/progress") {
      return Promise.resolve(response(retried ? { ...failedProgress, status: "queued", system_error: null, finished_at: null } : failedProgress));
    }
    if (input === "/api/v1/evaluation-runs/run-1/retry-scoring" && init?.method === "POST") {
      retried = true;
      return Promise.resolve(response({ ...failedProgress, status: "queued", system_error: null, finished_at: null }));
    }
    if (input === "/api/v1/evaluation-runs" && !init?.method) return Promise.resolve(response([]));
    throw new Error(`Unexpected request: ${input}`);
  }));
  renderPage("/runs/run-1");

  expect(await screen.findByText("answer_scoring_judge_rate_limit")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "重试评分" }));
  expect(screen.getByRole("heading", { name: "继续未完成的评分？" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "确认重试评分" }));

  await waitFor(() => expect(screen.getByText("排队中")).toBeInTheDocument());
  expect(retried).toBe(true);
});
