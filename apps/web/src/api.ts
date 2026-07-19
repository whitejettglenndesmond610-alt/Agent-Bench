export interface PlatformMeta {
  name: string;
  version: string;
  environment: string;
  status: string;
}

export type SessionRole = "read_only" | "operator";

export interface SessionInfo {
  role: SessionRole;
  expires_at: string;
  user_id: string;
  display_name: string;
  is_shared: boolean;
  workspace_enabled: boolean;
}

interface ValidationErrorDetail {
  loc?: Array<string | number>;
  msg?: string;
}

interface ErrorPayload {
  detail?:
    | {
        code?: string;
        message?: string;
      }
    | ValidationErrorDetail[];
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
    readonly fieldErrors: Record<string, string> = {}
  ) {
    super(message);
  }
}

function csrfToken(): string | undefined {
  const entry = document.cookie
    .split("; ")
    .find((cookie) => cookie.startsWith("agent_eval_csrf="));
  return entry ? decodeURIComponent(entry.split("=", 2)[1]) : undefined;
}

async function errorFromResponse(response: Response): Promise<ApiError> {
  let payload: ErrorPayload = {};
  try {
    payload = (await response.json()) as ErrorPayload;
  } catch {
    // The status text remains the fallback for non-JSON proxy errors.
  }
  if (Array.isArray(payload.detail)) {
    const fieldErrors: Record<string, string> = {};
    for (const error of payload.detail) {
      const field = [...(error.loc ?? [])].reverse().find((item) => typeof item === "string");
      if (field && error.msg) {
        fieldErrors[field] = error.msg.replace(/^Value error, /, "");
      }
    }
    const message = Object.values(fieldErrors)[0] || "请检查表单内容";
    return new ApiError(message, response.status, "validation_error", fieldErrors);
  }
  return new ApiError(
    payload.detail?.message || response.statusText || "请求失败",
    response.status,
    payload.detail?.code
  );
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      ...init?.headers
    }
  });

  if (!response.ok) {
    throw await errorFromResponse(response);
  }
  return response.json() as Promise<T>;
}

export async function fetchPlatformMeta(): Promise<PlatformMeta> {
  return requestJson<PlatformMeta>("/api/v1/meta");
}

export async function fetchSession(): Promise<SessionInfo | null> {
  const response = await fetch("/api/v1/auth/session", {
    credentials: "same-origin",
    headers: { Accept: "application/json" }
  });
  if (response.status === 401) {
    return null;
  }
  if (!response.ok) {
    throw await errorFromResponse(response);
  }
  return response.json() as Promise<SessionInfo>;
}

export async function login(role: SessionRole, password: string): Promise<SessionInfo> {
  return requestJson<SessionInfo>("/api/v1/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ role, password })
  });
}

export async function userLogin(username: string, password: string): Promise<SessionInfo> {
  return requestJson<SessionInfo>("/api/v1/auth/user-login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password })
  });
}

export async function logout(): Promise<void> {
  const token = csrfToken();
  const response = await fetch("/api/v1/auth/logout", {
    method: "POST",
    credentials: "same-origin",
    headers: token ? { "X-CSRF-Token": token } : {}
  });
  if (!response.ok) {
    throw await errorFromResponse(response);
  }
}

export type AgentStatus = "enabled" | "disabled";
export type AuthType = "none" | "bearer";
export type AgentVersionLifecycle = "draft" | "verified" | "active" | "retired";
export type PreflightStatus = "not_run" | "running" | "passed" | "failed";

export interface AgentSummary {
  id: string;
  name: string;
  provider: string;
  description: string | null;
  logo_url: string | null;
  status: AgentStatus;
  created_at: string;
  updated_at: string;
}

export interface AgentVersion {
  id: string;
  agent_id: string;
  version: number;
  endpoint_url: string;
  auth_type: AuthType;
  token_configured: boolean;
  request_template: string;
  answer_jsonpath: string;
  citations_jsonpath: string | null;
  timeout_seconds: number;
  concurrency: number;
  max_prompt_characters: number | null;
  lifecycle_status: AgentVersionLifecycle;
  preflight_status: PreflightStatus;
  preflight_report: PreflightCheck[];
  preflight_at: string | null;
  enabled: boolean;
  created_at: string;
}

export interface AgentDetail extends AgentSummary {
  versions: AgentVersion[];
}

export interface AgentCreateInput {
  name: string;
  provider: string;
  description?: string;
  logo_url?: string;
}

export interface AgentVersionInput {
  endpoint_url: string;
  auth_type: AuthType;
  token?: string;
  request_template: string;
  answer_jsonpath: string;
  citations_jsonpath?: string;
  timeout_seconds: number;
  concurrency: number;
  max_prompt_characters?: number | null;
}

export interface ConnectionTestResult {
  success: boolean;
  status_code: number | null;
  latency_ms: number;
  request_body: Record<string, unknown>;
  raw_response: unknown;
  answer: string | null;
  citations: string[];
  error: string | null;
}

export interface PreflightCheck {
  key: string;
  label: string;
  status: "passed" | "failed" | "skipped";
  prompt_characters: number;
  status_code: number | null;
  latency_ms: number | null;
  error_category: string | null;
  error: string | null;
}

export interface AgentVersionPreflight {
  version_id: string;
  lifecycle_status: AgentVersionLifecycle;
  preflight_status: PreflightStatus;
  checks: PreflightCheck[];
  preflight_at: string;
}

function jsonMutation<T>(path: string, method: "POST" | "PATCH" | "PUT" | "DELETE", body?: unknown): Promise<T> {
  const token = csrfToken();
  return requestJson<T>(path, {
    method,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { "X-CSRF-Token": token } : {})
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) })
  });
}

async function voidMutation(path: string, method: "PUT" | "DELETE", body?: unknown): Promise<void> {
  const token = csrfToken();
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      ...(token ? { "X-CSRF-Token": token } : {})
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) })
  });
  if (!response.ok) throw await errorFromResponse(response);
}

export interface WorkspaceAgent {
  agent_id: string;
  agent_version_id: string;
  name: string;
  provider: string;
  description: string | null;
  logo_url: string | null;
  version: number;
  max_prompt_characters: number | null;
  supports_citations: boolean;
  is_favorite?: boolean;
}

export interface WorkspaceConversationAgent {
  agent_id: string;
  agent_version_id: string;
  name: string;
  provider: string;
  version: number;
}

export interface WorkspaceConversation {
  id: string;
  agent_version_id: string;
  title: string;
  revision: number;
  last_turn_no: number;
  archived_at: string | null;
  created_at: string;
  updated_at: string;
  agent: WorkspaceConversationAgent;
}

export interface WorkspaceConversationList {
  items: WorkspaceConversation[];
}

export interface WorkspaceMessage {
  id: string;
  turn_no: number;
  role: "user" | "assistant";
  variant_no: number;
  active: boolean;
  content: string;
  citations: string[];
  created_at: string;
}

export type WorkspaceAttemptStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "canceled";

export interface WorkspaceAttemptError {
  type: string;
  category: string;
}

export interface WorkspaceAttempt {
  id: string;
  conversation_id: string;
  user_message_id: string;
  generation_no: number;
  status: WorkspaceAttemptStatus;
  assistant_message_id: string | null;
  agent_version_id: string;
  error_category: string | null;
  assistant_message: WorkspaceMessage | null;
  http_attempt_count: number;
  last_http_status: number | null;
  latency_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  feedback?: "up" | "down" | null;
  bookmarked?: boolean;
  private_note?: string | null;
  error: WorkspaceAttemptError | null;
  cancel_requested_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface WorkspaceTurn {
  turn_no: number;
  user_message: WorkspaceMessage;
  assistant_messages: WorkspaceMessage[];
  attempts: WorkspaceAttempt[];
}

export interface WorkspaceConversationDetail extends WorkspaceConversation {
  turns: WorkspaceTurn[];
}

export interface WorkspaceSendResult {
  conversation_revision: number;
  user_message: WorkspaceMessage;
  attempt: WorkspaceAttempt;
}

export interface WorkspaceRegenerateResult {
  conversation_revision: number;
  attempt: WorkspaceAttempt;
}

export type WorkspaceComparisonStatus = "queued" | "running" | "canceling" | "completed";

export interface WorkspaceComparisonCounts {
  queued: number;
  running: number;
  succeeded: number;
  failed: number;
  canceled: number;
}

export interface WorkspaceComparisonItem {
  position: number;
  conversation_id: string;
  agent: WorkspaceConversationAgent;
  attempt: WorkspaceAttempt;
  evaluation_case_id: string | null;
}

export interface WorkspaceComparison {
  id: string;
  title: string;
  prompt: string;
  status: WorkspaceComparisonStatus;
  counts: WorkspaceComparisonCounts;
  revision?: number;
  archived_at?: string | null;
  cancel_requested_at: string | null;
  created_at: string;
  updated_at: string;
  items: WorkspaceComparisonItem[];
}

export interface WorkspaceComparisonList {
  items: WorkspaceComparison[];
}

export function fetchWorkspaceAgents(): Promise<WorkspaceAgent[]> {
  return requestJson<WorkspaceAgent[]>("/api/v1/workspace/agents");
}

export function setWorkspaceAgentFavorite(agentId: string, favorite: boolean): Promise<void> {
  return voidMutation(`/api/v1/workspace/agents/${agentId}/favorite`, favorite ? "PUT" : "DELETE");
}

export interface WorkspacePromptTemplate {
  id: string;
  name: string;
  content: string;
  revision: number;
  created_at: string;
  updated_at: string;
}

export interface WorkspacePromptTemplateList {
  items: WorkspacePromptTemplate[];
}

export function fetchWorkspacePromptTemplates(): Promise<WorkspacePromptTemplateList> {
  return requestJson<WorkspacePromptTemplateList>("/api/v1/workspace/prompt-templates");
}

export function fetchWorkspacePromptTemplate(templateId: string): Promise<WorkspacePromptTemplate> {
  return requestJson<WorkspacePromptTemplate>(`/api/v1/workspace/prompt-templates/${templateId}`);
}

export function createWorkspacePromptTemplate(input: {
  name: string;
  content: string;
}): Promise<WorkspacePromptTemplate> {
  return jsonMutation<WorkspacePromptTemplate>("/api/v1/workspace/prompt-templates", "POST", input);
}

export function updateWorkspacePromptTemplate(
  templateId: string,
  input: { expected_revision: number; name?: string; content?: string }
): Promise<WorkspacePromptTemplate> {
  return jsonMutation<WorkspacePromptTemplate>(
    `/api/v1/workspace/prompt-templates/${templateId}`,
    "PATCH",
    input
  );
}

export function deleteWorkspacePromptTemplate(
  templateId: string,
  expectedRevision: number
): Promise<void> {
  return voidMutation(`/api/v1/workspace/prompt-templates/${templateId}`, "DELETE", {
    expected_revision: expectedRevision
  });
}

export type WorkspaceExperienceCategory =
  | "analysis"
  | "retrieval_citations"
  | "structured_extraction"
  | "writing_instruction_following"
  | "data_reasoning"
  | "long_context_synthesis";

export interface WorkspaceExperienceTask {
  slug: string;
  title: string;
  summary: string;
  prompt: string;
  category: WorkspaceExperienceCategory;
  difficulty: "beginner" | "intermediate" | "advanced";
  recommended_mode: "chat" | "comparison";
  min_agents: number;
  max_agents: number;
  ranking_eligible: false;
}

export function fetchWorkspaceExperienceTasks(): Promise<WorkspaceExperienceTask[]> {
  return requestJson<WorkspaceExperienceTask[]>("/api/v1/workspace/experience-tasks");
}

export function fetchWorkspaceExperienceTask(slug: string): Promise<WorkspaceExperienceTask> {
  return requestJson<WorkspaceExperienceTask>(`/api/v1/workspace/experience-tasks/${slug}`);
}

export function fetchWorkspaceConversations(): Promise<WorkspaceConversationList> {
  return requestJson<WorkspaceConversationList>("/api/v1/workspace/conversations");
}

export function createWorkspaceConversation(input: {
  agent_version_id: string;
  title?: string;
}): Promise<WorkspaceConversation> {
  return jsonMutation<WorkspaceConversation>("/api/v1/workspace/conversations", "POST", input);
}

export function fetchWorkspaceConversation(
  conversationId: string
): Promise<WorkspaceConversationDetail> {
  return requestJson<WorkspaceConversationDetail>(
    `/api/v1/workspace/conversations/${conversationId}`
  );
}

export function updateWorkspaceConversation(
  conversationId: string,
  input: {
    title?: string;
    archived?: boolean;
    agent_version_id?: string;
    expected_revision: number;
  }
): Promise<WorkspaceConversation> {
  return jsonMutation<WorkspaceConversation>(
    `/api/v1/workspace/conversations/${conversationId}`,
    "PATCH",
    input
  );
}

export function sendWorkspaceMessage(
  conversationId: string,
  input: { content: string; idempotency_key: string; expected_revision: number }
): Promise<WorkspaceSendResult> {
  return jsonMutation<WorkspaceSendResult>(
    `/api/v1/workspace/conversations/${conversationId}/messages`,
    "POST",
    input
  );
}

export function regenerateWorkspaceMessage(
  conversationId: string,
  userMessageId: string,
  input: { idempotency_key: string; expected_revision: number }
): Promise<WorkspaceRegenerateResult> {
  return jsonMutation<WorkspaceRegenerateResult>(
    `/api/v1/workspace/conversations/${conversationId}/messages/${userMessageId}/regenerate`,
    "POST",
    input
  );
}

export function cancelWorkspaceAttempt(attemptId: string): Promise<WorkspaceAttempt> {
  return jsonMutation<WorkspaceAttempt>(
    `/api/v1/workspace/attempts/${attemptId}/cancel`,
    "POST",
    {}
  );
}

export function updateWorkspaceAttemptAnnotation(
  attemptId: string,
  input: {
    feedback?: "up" | "down" | null;
    bookmarked?: boolean;
    private_note?: string | null;
  }
): Promise<WorkspaceAttempt> {
  return jsonMutation<WorkspaceAttempt>(
    `/api/v1/workspace/attempts/${attemptId}/annotation`,
    "PATCH",
    input
  );
}

export function createWorkspaceComparison(input: {
  prompt: string;
  agent_version_ids: string[];
  idempotency_key: string;
  title?: string;
}): Promise<WorkspaceComparison> {
  return jsonMutation<WorkspaceComparison>("/api/v1/workspace/comparisons", "POST", input);
}

export function fetchWorkspaceComparisons(includeArchived = true): Promise<WorkspaceComparisonList> {
  return requestJson<WorkspaceComparisonList>(
    includeArchived
      ? "/api/v1/workspace/comparisons"
      : "/api/v1/workspace/comparisons?include_archived=false"
  );
}

export function fetchWorkspaceComparison(comparisonId: string): Promise<WorkspaceComparison> {
  return requestJson<WorkspaceComparison>(`/api/v1/workspace/comparisons/${comparisonId}`);
}

export function cancelWorkspaceComparison(comparisonId: string): Promise<WorkspaceComparison> {
  return jsonMutation<WorkspaceComparison>(
    `/api/v1/workspace/comparisons/${comparisonId}/cancel`,
    "POST",
    {}
  );
}

export function updateWorkspaceComparison(
  comparisonId: string,
  input: { expected_revision: number; title?: string; archived?: boolean }
): Promise<WorkspaceComparison> {
  return jsonMutation<WorkspaceComparison>(
    `/api/v1/workspace/comparisons/${comparisonId}`,
    "PATCH",
    input
  );
}

export function fetchAgents(): Promise<AgentSummary[]> {
  return requestJson<AgentSummary[]>("/api/v1/agents");
}

export function fetchAgent(agentId: string): Promise<AgentDetail> {
  return requestJson<AgentDetail>(`/api/v1/agents/${agentId}`);
}

export function createAgent(input: AgentCreateInput): Promise<AgentSummary> {
  return jsonMutation<AgentSummary>("/api/v1/agents", "POST", input);
}

export function createAgentVersion(
  agentId: string,
  input: AgentVersionInput
): Promise<AgentVersion> {
  return jsonMutation<AgentVersion>(`/api/v1/agents/${agentId}/versions`, "POST", input);
}

export function testAgentConnection(
  input: AgentVersionInput & { prompt: string }
): Promise<ConnectionTestResult> {
  return jsonMutation<ConnectionTestResult>("/api/v1/agents/test-connection", "POST", input);
}

export function preflightAgentVersion(versionId: string): Promise<AgentVersionPreflight> {
  return jsonMutation<AgentVersionPreflight>(`/api/v1/agent-versions/${versionId}/preflight`, "POST", {});
}

export function promoteAgentVersion(versionId: string): Promise<AgentVersion> {
  return jsonMutation<AgentVersion>(`/api/v1/agent-versions/${versionId}/promote`, "POST", {});
}

export type RunStatus =
  | "draft"
  | "queued"
  | "running"
  | "paused"
  | "scoring"
  | "completed"
  | "published"
  | "system_failed"
  | "canceled";

export type RunStage =
  | "preparing"
  | "inference"
  | "citation_fetch"
  | "judging"
  | "aggregating"
  | "ready_to_publish"
  | "publishing";

export interface RunState {
  id: string;
  experiment_id?: string | null;
  experiment_position?: number | null;
  name: string;
  status: RunStatus;
  desired_state: "running" | "paused" | "canceled";
  stage: RunStage;
  profile_key: string;
  run_scope: RunScope;
  target_ability: Ability | null;
  ranking_eligible: boolean;
  heartbeat_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  system_error: string | null;
  created_at: string;
}

export interface RunListItem extends RunState {
  agent_count: number;
  total: number;
  completed: number;
  failed: number;
  retried: number;
}

export interface RunAgentProgress {
  run_agent_id: string;
  agent_id: string;
  agent_version_id: string;
  agent_name: string;
  provider: string;
  version: number;
  total: number;
  completed: number;
  failed: number;
  retried: number;
  processed: number;
  failure_categories: Record<string, number>;
  validity_status: ResultValidityStatus;
  validity_reason: string | null;
  configuration_failure_count: number;
  configuration_failure_rate: number;
  result: RunAgentResultSummary | null;
}

export type ResultValidityStatus = "pending" | "valid" | "invalid";

export interface RunAgentResultSummary {
  overall_score: number;
  ability_scores: Partial<Record<Ability, number>>;
  answer_quality: number;
  citation_quality: number;
  success_rate: number;
  anomaly_rate: number;
  http_call_count: number;
  validity_status: ResultValidityStatus;
  validity_reason: string | null;
  configuration_failure_count?: number;
  configuration_failure_rate?: number;
  total_input_tokens?: number | null;
  total_output_tokens?: number | null;
  total_tokens?: number | null;
  token_usage_available_count?: number;
  token_usage_missing_count?: number;
  latency_p50_ms?: number | null;
  latency_p95_ms?: number | null;
  latency_sample_count?: number;
}

export interface RunProgress extends RunState {
  total: number;
  completed: number;
  failed: number;
  retried: number;
  processed: number;
  progress_percent: number;
  agents: RunAgentProgress[];
}

export interface RunAgentOption {
  agent_id: string;
  agent_version_id: string;
  name: string;
  provider: string;
  version: number;
  lifecycle_status: AgentVersionLifecycle;
  max_prompt_characters: number | null;
  supports_citations: boolean;
  profile_compatibility: RunProfileCompatibility[];
}

export interface RunProfileCompatibility {
  profile_key: string;
  compatible: boolean;
  oversized_question_count: number;
  max_question_characters: number;
  citation_required_count: number;
  blocking_reasons: string[];
  warnings: string[];
}

export type Ability =
  | "reasoning"
  | "retrieval"
  | "data_reasoning"
  | "instruction_following"
  | "long_context";
export type RunScope = "comprehensive" | "specialist" | "smoke";

export type EvaluationCaseStatus = "draft" | "reviewed" | "published";
export type EvaluationQuestionType = "short_answer" | "open_ended";

export interface EvaluationCaseSourceAgent {
  agent_id: string;
  agent_version_id: string;
  name: string;
  provider: string;
  version: number;
}

export interface EvaluationCaseSource {
  attempt_id: string;
  conversation_id: string;
  comparison_id: string | null;
  user_message: string;
  candidate_answer: string;
  citations: string[];
  generation_no: number;
  prompt_format: string;
  prompt_hash: string;
  latency_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  artifact_sha256: string | null;
  agent: EvaluationCaseSourceAgent;
}

export interface EvaluationCase {
  id: string;
  status: EvaluationCaseStatus;
  revision: number;
  prompt: string;
  reference_answer: string | null;
  language: "zh" | "en" | null;
  ability: Ability | null;
  question_type: EvaluationQuestionType;
  requires_citations: boolean;
  difficulty: string | null;
  domain: string | null;
  review_notes: string | null;
  standalone_verified: boolean;
  reference_answer_verified: boolean;
  rights_verified: boolean;
  sensitive_data_verified: boolean;
  source_hash: string;
  record_hash: string | null;
  reviewed_at: string | null;
  published_at: string | null;
  created_at: string;
  updated_at: string;
  source: EvaluationCaseSource;
}

export interface EvaluationCaseList {
  items: EvaluationCase[];
}

export interface EvaluationCasePatchInput {
  expected_revision: number;
  prompt?: string | null;
  reference_answer?: string | null;
  language?: "zh" | "en" | null;
  ability?: Ability | null;
  question_type?: EvaluationQuestionType | null;
  requires_citations?: boolean | null;
  difficulty?: string | null;
  domain?: string | null;
  review_notes?: string | null;
}

export function createEvaluationCase(input: {
  source_attempt_id: string;
  idempotency_key: string;
}): Promise<EvaluationCase> {
  return jsonMutation<EvaluationCase>("/api/v1/evaluation-cases", "POST", input);
}

export function fetchEvaluationCases(): Promise<EvaluationCaseList> {
  return requestJson<EvaluationCaseList>("/api/v1/evaluation-cases");
}

export function fetchPublishedEvaluationCases(): Promise<EvaluationCaseList> {
  return requestJson<EvaluationCaseList>("/api/v1/evaluation-cases?status=published&limit=100");
}

export function fetchEvaluationCase(caseId: string): Promise<EvaluationCase> {
  return requestJson<EvaluationCase>(`/api/v1/evaluation-cases/${caseId}`);
}

export function updateEvaluationCase(
  caseId: string,
  input: EvaluationCasePatchInput
): Promise<EvaluationCase> {
  return jsonMutation<EvaluationCase>(`/api/v1/evaluation-cases/${caseId}`, "PATCH", input);
}

export function reviewEvaluationCase(
  caseId: string,
  input: {
    expected_revision: number;
    standalone_verified: true;
    reference_answer_verified: true;
    rights_verified: true;
    sensitive_data_verified: true;
  }
): Promise<EvaluationCase> {
  return jsonMutation<EvaluationCase>(`/api/v1/evaluation-cases/${caseId}/review`, "POST", input);
}

export function publishEvaluationCase(
  caseId: string,
  input: { expected_revision: number; confirm_curated_only: true }
): Promise<EvaluationCase> {
  return jsonMutation<EvaluationCase>(`/api/v1/evaluation-cases/${caseId}/publish`, "POST", input);
}

export type DatasetReleaseStatus = "draft" | "approved" | "published";

export interface DatasetReleaseCase {
  id: string;
  position: number;
  prompt: string;
  language: "zh" | "en";
  ability: Ability;
  question_type: EvaluationQuestionType;
  record_hash: string;
}

export interface DatasetReleaseVersion {
  id: string;
  benchmark_name: string;
  language: "zh" | "en";
  ability: Ability;
  version: number;
  question_count: number;
  content_hash: string;
  run_eligible: boolean;
}

export interface DatasetRelease {
  id: string;
  name: string;
  description: string | null;
  status: DatasetReleaseStatus;
  revision: number;
  manifest_hash: string | null;
  approved_by_user_id: string | null;
  approved_at: string | null;
  published_by_user_id: string | null;
  published_at: string | null;
  created_at: string;
  updated_at: string;
  cases: DatasetReleaseCase[];
  dataset_versions: DatasetReleaseVersion[];
}

export interface DatasetReleaseList {
  items: DatasetRelease[];
}

export function createDatasetRelease(input: {
  name: string;
  description?: string | null;
  evaluation_case_ids: string[];
  idempotency_key: string;
}): Promise<DatasetRelease> {
  return jsonMutation<DatasetRelease>("/api/v1/dataset-releases", "POST", input);
}

export function fetchDatasetReleases(): Promise<DatasetReleaseList> {
  return requestJson<DatasetReleaseList>("/api/v1/dataset-releases");
}

export function fetchDatasetRelease(releaseId: string): Promise<DatasetRelease> {
  return requestJson<DatasetRelease>(`/api/v1/dataset-releases/${releaseId}`);
}

export function approveDatasetRelease(
  releaseId: string,
  input: { expected_revision: number; confirm_case_selection: true }
): Promise<DatasetRelease> {
  return jsonMutation<DatasetRelease>(`/api/v1/dataset-releases/${releaseId}/approve`, "POST", input);
}

export function publishDatasetRelease(
  releaseId: string,
  input: { expected_revision: number; confirm_not_run_eligible: true }
): Promise<DatasetRelease> {
  return jsonMutation<DatasetRelease>(`/api/v1/dataset-releases/${releaseId}/publish`, "POST", input);
}

export interface RunDatasetOption {
  benchmark_id: string;
  dataset_version_id: string;
  name: string;
  ability: Ability;
  language: "zh" | "en";
  version: number;
  question_count: number;
  cohort_key: string;
}

export interface RunProfileOption {
  key: string;
  name: string;
  description: string;
  scope: RunScope;
  target_ability: Ability | null;
  dataset_version_ids: string[];
  question_count: number;
  benchmark_count: number;
  publishes_to: string[];
  ranking_eligible: boolean;
  datasets: RunDatasetOption[];
  score_config_id: string | null;
  ability_weights: Partial<Record<Ability, number>>;
}

export interface RunOptions {
  agents: RunAgentOption[];
  datasets: RunDatasetOption[];
  dataset_version_ids: string[];
  question_count: number;
  profiles: RunProfileOption[];
  default_profile_key: string;
  judge_config_id: string | null;
  judge_model: string | null;
  score_config_id: string | null;
  ability_weights: Partial<Record<Ability, number>>;
  ready: boolean;
  issues: string[];
}

export interface RunCreateInput {
  name: string;
  agent_version_ids: [string];
  profile_key: string;
  dataset_version_ids?: string[];
  judge_config_id: string;
  score_config_id: string;
  force_retest?: boolean;
}

export interface RunCreateResult {
  id: string;
  experiment_id?: string | null;
  experiment_position?: number | null;
  name: string;
  status: RunStatus;
  question_count: number;
  execution_item_count: number;
}

export function fetchRunOptions(): Promise<RunOptions> {
  return requestJson<RunOptions>("/api/v1/evaluation-runs/options");
}

export function fetchRuns(): Promise<RunListItem[]> {
  return requestJson<RunListItem[]>("/api/v1/evaluation-runs");
}

export function fetchRunProgress(runId: string): Promise<RunProgress> {
  return requestJson<RunProgress>(`/api/v1/evaluation-runs/${runId}/progress`);
}

export function createRun(input: RunCreateInput): Promise<RunCreateResult> {
  return jsonMutation<RunCreateResult>("/api/v1/evaluation-runs", "POST", input);
}

export interface DashboardProfileSummary {
  key: string;
  name: string;
  ready: boolean;
  question_count: number;
}

export interface DashboardActiveProfile extends DashboardProfileSummary {
  dataset_version_count: number;
  language_counts: Record<"zh" | "en", number>;
  ability_counts: Record<Ability, number>;
  ability_weights: Record<Ability, number>;
}

export interface DashboardSummary {
  active_profile: DashboardActiveProfile;
  ranking_profiles: DashboardProfileSummary[];
  judge_model: string | null;
}

export function fetchDashboardSummary(): Promise<DashboardSummary> {
  return requestJson<DashboardSummary>("/api/v1/dashboard/summary");
}

export type ExecutionStatus =
  | "pending"
  | "in_progress"
  | "answered"
  | "failed"
  | "scored"
  | "canceled";

export type EvidenceAttemptStatus = "in_progress" | "succeeded" | "failed" | "abandoned";
export type CitationFetchStatus =
  | "pending"
  | "fetching"
  | "fetched"
  | "blocked"
  | "unreachable"
  | "unsupported";
export type EvidenceArtifactKind = "agent_response" | "citation_content" | "judge_response";

export interface RunEvidenceArtifactMetadata {
  artifact_id: string;
  kind: EvidenceArtifactKind;
  sha256: string;
  size_bytes: number;
  expires_at: string;
  retained: boolean;
}

export interface RunEvidenceScoreSummary {
  score_id: string;
  answer_score: number;
  citation_score: number | null;
  final_score: number;
}

export interface RunEvidenceListItem {
  execution_item_id: string;
  run_agent_id: string;
  agent_id: string;
  agent_version_id: string;
  agent_name: string;
  provider: string;
  version: number;
  question_id: string;
  question_external_id: string;
  question_type: "multiple_choice" | "short_answer" | "open_ended";
  benchmark_id: string;
  benchmark_name: string;
  ability: Ability;
  language: "zh" | "en";
  requires_citations: boolean;
  difficulty: string | null;
  domain: string | null;
  status: ExecutionStatus;
  error_type: string | null;
  answer_preview: string | null;
  attempt_count: number;
  latency_ms: number | null;
  score: RunEvidenceScoreSummary | null;
  created_at: string;
  updated_at: string;
}

export interface EvaluationRunEvidenceList {
  run_id: string;
  items: RunEvidenceListItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface RunEvidenceAttempt {
  attempt_id: string;
  attempt_no: number;
  status: EvidenceAttemptStatus;
  started_at: string;
  finished_at: string | null;
  status_code: number | null;
  latency_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  error_type: string | null;
}

export interface RunEvidenceCitation {
  citation_id: string;
  position: number;
  original_url: string;
  final_url: string | null;
  fetch_status: CitationFetchStatus;
  http_status: number | null;
  authenticity_score: number | null;
  support_score: number | null;
  judge_reason: string | null;
  content_artifact: RunEvidenceArtifactMetadata | null;
  judge_artifact: RunEvidenceArtifactMetadata | null;
}

export interface RunEvidenceDetail extends RunEvidenceListItem {
  prompt: string;
  choices: Array<{ label: string; text: string }> | null;
  reference_answer: string;
  candidate_answer: string | null;
  candidate_answer_status: "available" | "not_recorded" | "expired" | "unavailable";
  attempts: RunEvidenceAttempt[];
  score: (RunEvidenceScoreSummary & {
    judge_reason: string | null;
    judge_artifact: RunEvidenceArtifactMetadata | null;
  }) | null;
  citations: RunEvidenceCitation[];
  response_artifact: RunEvidenceArtifactMetadata | null;
}

export interface RunEvidenceFilters {
  run_agent_id?: string;
  status?: ExecutionStatus;
  ability?: Ability;
  language?: "zh" | "en";
  error_type?: string;
  min_score?: number;
  max_score?: number;
  limit?: number;
  offset?: number;
}

export function fetchRunEvidence(
  runId: string,
  filters: RunEvidenceFilters = {}
): Promise<EvaluationRunEvidenceList> {
  const parameters = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== "") parameters.set(key, String(value));
  }
  const query = parameters.toString();
  return requestJson<EvaluationRunEvidenceList>(
    `/api/v1/evaluation-runs/${runId}/evidence${query ? `?${query}` : ""}`
  );
}

export function fetchRunEvidenceDetail(
  runId: string,
  executionItemId: string
): Promise<RunEvidenceDetail> {
  return requestJson<RunEvidenceDetail>(
    `/api/v1/evaluation-runs/${runId}/evidence/${executionItemId}`
  );
}

export interface EvaluationExperimentRunCounts {
  draft: number;
  queued: number;
  running: number;
  paused: number;
  scoring: number;
  completed: number;
  published: number;
  system_failed: number;
  canceled: number;
}

export interface EvaluationExperimentSummary {
  id: string;
  name: string;
  creator: { user_id: string | null; subject_key: string; display_name: string };
  status: RunStatus;
  protocol_hash: string;
  profile_key: string;
  run_scope: RunScope;
  target_ability: Ability | null;
  member_count: number;
  run_counts: EvaluationExperimentRunCounts;
  total: number;
  processed: number;
  completed: number;
  failed: number;
  progress_percent: number;
  created_at: string;
  updated_at: string;
}

export interface EvaluationExperimentMember {
  position: number;
  run_id: string;
  run_name: string;
  status: RunStatus;
  stage: RunStage;
  agent_id: string;
  agent_version_id: string;
  agent_name: string;
  provider: string;
  version: number;
  total: number;
  processed: number;
  completed: number;
  failed: number;
  retried: number;
  progress_percent: number;
  result: RunAgentResultSummary | null;
  metric_scores: Record<string, number>;
  metric_ranks: Record<string, number>;
}

export interface EvaluationExperimentDetail extends EvaluationExperimentSummary {
  members: EvaluationExperimentMember[];
  comparison_metrics: Array<{ metric: string; protocol_hash: string; compared_count: number }>;
}

export interface EvaluationExperimentCreateInput {
  name: string;
  idempotency_key: string;
  agent_version_ids: string[];
  profile_key: string;
  dataset_version_ids?: string[];
  judge_config_id: string;
  score_config_id: string;
  force_retest?: boolean;
}

export function createEvaluationExperiment(
  input: EvaluationExperimentCreateInput
): Promise<EvaluationExperimentDetail> {
  return jsonMutation<EvaluationExperimentDetail>("/api/v1/evaluation-experiments", "POST", input);
}

export function fetchEvaluationExperiments(): Promise<EvaluationExperimentSummary[]> {
  return requestJson<EvaluationExperimentSummary[]>("/api/v1/evaluation-experiments");
}

export function fetchEvaluationExperiment(
  experimentId: string
): Promise<EvaluationExperimentDetail> {
  return requestJson<EvaluationExperimentDetail>(
    `/api/v1/evaluation-experiments/${experimentId}`
  );
}

export interface RunBatchCreateResult {
  runs: RunCreateResult[];
}

export interface RunBatchCreateInput {
  name: string;
  agent_version_ids: string[];
  profile_key: string;
  dataset_version_ids?: string[];
  judge_config_id: string;
  score_config_id: string;
  force_retest?: boolean;
}

export function createRunsBatch(input: RunBatchCreateInput): Promise<RunBatchCreateResult> {
  return jsonMutation<RunBatchCreateResult>("/api/v1/evaluation-runs/batch", "POST", input);
}

export type AgentEvaluationMetricStatus =
  | "not_evaluated"
  | "queued"
  | "running"
  | "evaluated"
  | "needs_retest"
  | "failed"
  | "invalid";

export interface AgentEvaluationMetric {
  status: AgentEvaluationMetricStatus;
  score: number | null;
  run_id: string | null;
  source_version: number | null;
  updated_at: string | null;
  message?: string | null;
}

export interface AgentEvaluationStatus {
  agent_id: string;
  agent_version_id: string;
  agent_name: string;
  provider: string;
  version: number;
  max_prompt_characters: number | null;
  supports_citations: boolean;
  comprehensive: AgentEvaluationMetric;
  reasoning: AgentEvaluationMetric;
  retrieval: AgentEvaluationMetric;
  data_reasoning: AgentEvaluationMetric;
  instruction_following: AgentEvaluationMetric;
  long_context: AgentEvaluationMetric;
}

export function fetchAgentEvaluationStatuses(): Promise<AgentEvaluationStatus[]> {
  return requestJson<AgentEvaluationStatus[]>("/api/v1/evaluation-runs/agent-statuses");
}

export function cancelRun(runId: string): Promise<RunState> {
    return jsonMutation<RunState>(`/api/v1/evaluation-runs/${runId}/cancel`, "POST", {});
}

export function retryRunScoring(runId: string): Promise<RunState> {
  return jsonMutation<RunState>(`/api/v1/evaluation-runs/${runId}/retry-scoring`, "POST", {});
}

export interface BenchmarkRanking {
  benchmark_id: string;
  name: string;
  ability: Ability;
  language: "zh" | "en";
  score: number;
}

export interface AgentRanking {
  rank: number;
  run_agent_id: string;
  agent_id: string;
  agent_version_id: string;
  agent_name: string;
  provider: string;
  version: number;
  overall_score: number;
  ability_scores: Partial<Record<Ability, number>>;
  language_scores: Record<"zh" | "en", number>;
  benchmark_scores: BenchmarkRanking[];
  answer_quality: number;
  citation_quality: number;
  success_rate: number;
  anomaly_rate: number;
  http_call_count: number;
  total_input_tokens?: number | null;
  total_output_tokens?: number | null;
  total_tokens?: number | null;
  latency_p50_ms?: number | null;
  latency_p95_ms?: number | null;
}

export interface LeaderboardSnapshot {
  id: string;
  run_id: string;
  run_name: string;
  dataset_bundle_hash: string;
  published_at: string;
  rankings: AgentRanking[];
}

export interface LeaderboardHistoryItem {
  id: string;
  run_id: string;
  run_name: string;
  published_at: string;
  agent_count: number;
  is_current: boolean;
}

export function fetchCurrentLeaderboard(): Promise<LeaderboardSnapshot> {
  return requestJson<LeaderboardSnapshot>("/api/v1/leaderboards/current");
}

export function fetchLeaderboardHistory(): Promise<LeaderboardHistoryItem[]> {
  return requestJson<LeaderboardHistoryItem[]>("/api/v1/leaderboards/history");
}

export function fetchLeaderboardSnapshot(snapshotId: string): Promise<LeaderboardSnapshot> {
  return requestJson<LeaderboardSnapshot>(`/api/v1/leaderboards/${snapshotId}`);
}

export type GlobalLeaderboardMetric = "overall" | Ability;

export interface GlobalRankedAgent {
  rank: number;
  score: number;
  agent_id: string;
  agent_version_id: string;
  agent_name: string;
  provider: string;
  version: number;
  status: "current" | "needs_retest";
  source_snapshot_id: string;
  source_run_id: string;
  source_run_name: string;
  source_profile_key: string;
  source_published_at: string;
  benchmark_count: number;
  success_rate: number;
  anomaly_rate: number;
  http_call_count: number;
  benchmark_scores: BenchmarkRanking[];
}

export interface GlobalUnrankedAgent {
  agent_id: string;
  agent_version_id: string | null;
  agent_name: string;
  provider: string;
  version: number | null;
  status: "not_evaluated";
}

export interface GlobalLeaderboard {
  metric: GlobalLeaderboardMetric;
  label: string;
  protocol_hash: string | null;
  generated_at: string;
  protocol_published_at: string | null;
  eligible_agent_count: number;
  benchmark_count: number;
  question_count: number;
  ranked_agents: GlobalRankedAgent[];
  unranked_agents: GlobalUnrankedAgent[];
}

export function fetchGlobalLeaderboard(metric: GlobalLeaderboardMetric): Promise<GlobalLeaderboard> {
  return requestJson<GlobalLeaderboard>(`/api/v1/leaderboards/global/${metric}`);
}

export interface GlobalMetricProtocol {
  metric: GlobalLeaderboardMetric;
  label: string;
  protocol_hash: string | null;
  published_at: string | null;
  benchmark_count: number;
  question_count: number;
  ranked_agent_count: number;
}

export interface GlobalOverviewMetric {
  status: AgentEvaluationMetricStatus;
  rank: number | null;
  score: number | null;
  source_agent_version_id: string | null;
  source_version: number | null;
  source_snapshot_id: string | null;
  source_run_id: string | null;
  source_run_name: string | null;
  source_profile_key: string | null;
  source_published_at: string | null;
  benchmark_count: number;
  success_rate: number | null;
  anomaly_rate: number | null;
  http_call_count: number | null;
  message?: string | null;
  total_input_tokens?: number | null;
  total_output_tokens?: number | null;
  total_tokens?: number | null;
  token_usage_available_count?: number;
  token_usage_missing_count?: number;
  latency_p50_ms?: number | null;
  latency_p95_ms?: number | null;
  latency_sample_count?: number;
  benchmark_scores: BenchmarkRanking[];
}

export interface GlobalOverviewAgent {
  agent_id: string;
  agent_version_id: string | null;
  agent_name: string;
  provider: string;
  version: number | null;
  metrics: Record<GlobalLeaderboardMetric, GlobalOverviewMetric>;
}

export interface GlobalLeaderboardOverview {
  generated_at: string;
  eligible_agent_count: number;
  protocols: GlobalMetricProtocol[];
  agents: GlobalOverviewAgent[];
}

export interface AgentScoreHistoryPoint {
  score: number;
  protocol_hash: string;
  protocol_matches_current: boolean;
  is_current_source: boolean;
  agent_version_id: string;
  version: number;
  source_snapshot_id: string;
  source_run_id: string;
  source_run_name: string;
  source_profile_key: string;
  source_published_at: string;
  benchmark_count: number;
}

export interface AgentLeaderboardDetail {
  agent_id: string;
  agent_version_id: string | null;
  agent_name: string;
  provider: string;
  description: string | null;
  logo_url: string | null;
  version: number | null;
  metrics: Record<GlobalLeaderboardMetric, GlobalOverviewMetric>;
  score_history: Record<GlobalLeaderboardMetric, AgentScoreHistoryPoint[]>;
}

export function fetchGlobalLeaderboardOverview(): Promise<GlobalLeaderboardOverview> {
  return requestJson<GlobalLeaderboardOverview>("/api/v1/leaderboards/global/overview");
}

export function fetchAgentLeaderboardDetail(agentId: string): Promise<AgentLeaderboardDetail> {
  return requestJson<AgentLeaderboardDetail>(
    `/api/v1/leaderboards/agents/${agentId}?history_limit=50`
  );
}

export function fetchGlobalLeaderboardComparison(
  agentIds: string[]
): Promise<GlobalLeaderboardOverview> {
  const parameters = new URLSearchParams();
  for (const agentId of agentIds) parameters.append("agent_ids", agentId);
  return requestJson<GlobalLeaderboardOverview>(
    `/api/v1/leaderboards/global/compare?${parameters.toString()}`
  );
}

export function fetchLeaderboardComparison(
  snapshotId: string,
  agentVersionIds: string[]
): Promise<LeaderboardSnapshot> {
  const parameters = new URLSearchParams();
  for (const versionId of agentVersionIds) {
    parameters.append("agent_version_ids", versionId);
  }
  return requestJson<LeaderboardSnapshot>(
    `/api/v1/leaderboards/${snapshotId}/compare?${parameters.toString()}`
  );
}
