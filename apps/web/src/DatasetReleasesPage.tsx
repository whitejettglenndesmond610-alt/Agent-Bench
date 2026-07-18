import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import {
  approveDatasetRelease,
  ApiError,
  createDatasetRelease,
  DatasetRelease,
  DatasetReleaseList,
  EvaluationCase,
  fetchDatasetRelease,
  fetchDatasetReleases,
  fetchPublishedEvaluationCases,
  publishDatasetRelease
} from "./api";
import { abilityMetrics } from "./metrics";

const releasesKey = (userId: string) => ["dataset-releases", userId] as const;
const releaseKey = (userId: string, releaseId: string) =>
  ["dataset-releases", userId, releaseId] as const;
const casesKey = (userId: string) =>
  ["workspace", userId, "evaluation-cases", "published"] as const;

const statusLabels: Record<DatasetRelease["status"], string> = {
  draft: "待批准",
  approved: "已批准",
  published: "已发布"
};

function formatTime(value: string | null): string {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false
  }).format(date);
}

function abilityLabel(ability: EvaluationCase["ability"]): string {
  return abilityMetrics.find((item) => item.key === ability)?.label ?? ability ?? "未设置";
}

function ReleaseHistory({ releases, activeId }: { releases: DatasetRelease[]; activeId?: string }) {
  return (
    <aside className="dataset-release-history" aria-label="数据集发布历史">
      <header><span>RELEASE ARCHIVE</span><strong>发布批次</strong><Link to="/dataset-releases">+ 新建批次</Link></header>
      {releases.length === 0 && <p>暂无发布批次</p>}
      <ol>
        {releases.map((release) => (
          <li className={release.id === activeId ? "is-active" : ""} key={release.id}>
            <Link to={`/dataset-releases/${release.id}`}>
              <strong>{release.name}</strong>
              <span className={`release-status is-${release.status}`}>{statusLabels[release.status]}</span>
              <small>{release.cases.length} CASES · REV {release.revision}</small>
              <time dateTime={release.updated_at}>{formatTime(release.updated_at)}</time>
            </Link>
          </li>
        ))}
      </ol>
    </aside>
  );
}

function CreateRelease({
  cases,
  loading,
  error,
  canPublish,
  onCreated
}: {
  cases: EvaluationCase[];
  loading: boolean;
  error: string | null;
  canPublish: boolean;
  onCreated: (release: DatasetRelease) => void;
}) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const idempotencyKeyRef = useRef(crypto.randomUUID());
  const mutation = useMutation({
    mutationFn: () => createDatasetRelease({
      name: name.trim(),
      description: description.trim() || null,
      evaluation_case_ids: selectedIds,
      idempotency_key: idempotencyKeyRef.current
    }),
    onSuccess: onCreated
  });

  function toggle(caseId: string) {
    idempotencyKeyRef.current = crypto.randomUUID();
    setSelectedIds((current) => current.includes(caseId)
      ? current.filter((id) => id !== caseId)
      : [...current, caseId]);
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!name.trim() || selectedIds.length === 0 || !canPublish) return;
    mutation.mutate();
  }

  return (
    <form className="dataset-release-builder" onSubmit={submit}>
      <header>
        <div><span>CURATED CASE BATCH</span><h2>建立正式数据集发布草稿</h2></div>
        <b>{selectedIds.length} SELECTED</b>
      </header>
      <div className="release-builder-fields">
        <label><span>发布名称</span><input aria-label="发布名称" value={name} maxLength={160} onChange={(event) => { idempotencyKeyRef.current = crypto.randomUUID(); setName(event.target.value); }} /></label>
        <label><span>说明</span><textarea aria-label="发布说明" rows={3} value={description} maxLength={10_000} onChange={(event) => { idempotencyKeyRef.current = crypto.randomUUID(); setDescription(event.target.value); }} /></label>
      </div>
      <fieldset>
        <legend>选择已发布精选案例 <span>{cases.length} AVAILABLE</span></legend>
        {loading && <p>正在读取精选案例</p>}
        {error && <p className="is-error" role="alert">精选案例读取失败：{error}</p>}
        {!loading && !error && cases.length === 0 && <p>当前没有可发布的精选案例。请先完成案例复核与精选案例发布。</p>}
        <div className="release-case-grid">
          {cases.map((item) => {
            const selected = selectedIds.includes(item.id);
            return (
              <label className={selected ? "is-selected" : ""} key={item.id}>
                <input type="checkbox" checked={selected} aria-label={`选择案例 ${item.prompt}`} onChange={() => toggle(item.id)} />
                <span><strong>{item.prompt}</strong><small>{abilityLabel(item.ability)} · {item.language?.toUpperCase()} · {item.question_type}</small></span>
                <b>{selected ? String(selectedIds.indexOf(item.id) + 1).padStart(2, "0") : "+"}</b>
              </label>
            );
          })}
        </div>
      </fieldset>
      <section className="release-isolation-note">
        <strong>默认隔离策略</strong>
        <p>本流程生成冻结数据集，但保持不可运行。它不会创建 Run Profile、评测任务、快照或排行榜结果。</p>
      </section>
      {mutation.error && <div className="result-banner is-error" role="alert">{mutation.error.message}</div>}
      {canPublish ? (
        <button className="button" type="submit" disabled={mutation.isPending || !name.trim() || selectedIds.length === 0}>{mutation.isPending ? "正在建稿" : "建立发布草稿"}</button>
      ) : <small className="release-named-operator-note">共享运营会话不能建立正式发布批次，请使用具名运营账号。</small>}
    </form>
  );
}

function ReleaseDetail({
  release,
  canPublish,
  onUpdated,
  onRevisionConflict
}: {
  release: DatasetRelease;
  canPublish: boolean;
  onUpdated: (release: DatasetRelease) => void;
  onRevisionConflict: () => void;
}) {
  const [approveConfirmed, setApproveConfirmed] = useState(false);
  const [publishConfirmed, setPublishConfirmed] = useState(false);
  const approveMutation = useMutation({
    mutationFn: () => approveDatasetRelease(release.id, {
      expected_revision: release.revision,
      confirm_case_selection: true
    }),
    onSuccess: onUpdated,
    onError: (error) => {
      if (error instanceof ApiError && error.code === "revision_conflict") {
        onRevisionConflict();
      }
    }
  });
  const publishMutation = useMutation({
    mutationFn: () => publishDatasetRelease(release.id, {
      expected_revision: release.revision,
      confirm_not_run_eligible: true
    }),
    onSuccess: onUpdated,
    onError: (error) => {
      if (error instanceof ApiError && error.code === "revision_conflict") {
        onRevisionConflict();
      }
    }
  });
  const error = approveMutation.error ?? publishMutation.error;

  return (
    <article className="dataset-release-detail">
      <header>
        <div><span>RELEASE {release.id.slice(0, 8).toUpperCase()}</span><h2>{release.name}</h2><p>{release.description || "无补充说明"}</p></div>
        <b className={`release-status is-${release.status}`}>{statusLabels[release.status]}</b>
      </header>
      {error && <div className="result-banner is-error" role="alert">{error.message}</div>}
      <dl className="release-ledger">
        <div><dt>REVISION</dt><dd>{release.revision}</dd></div>
        <div><dt>CASES</dt><dd>{release.cases.length}</dd></div>
        <div><dt>DATASETS</dt><dd>{release.dataset_versions.length}</dd></div>
        <div><dt>APPROVED</dt><dd>{formatTime(release.approved_at)}</dd></div>
      </dl>
      <section className="release-manifest">
        <header><span>CASE MANIFEST</span><strong>冻结案例清单</strong></header>
        <ol>
          {release.cases.map((item) => (
            <li key={item.id}>
              <span>{String(item.position).padStart(2, "0")}</span>
              <div><strong>{item.prompt}</strong><small>{abilityLabel(item.ability)} · {item.language.toUpperCase()} · {item.question_type}</small></div>
              <code>{item.record_hash.slice(0, 12)}</code>
            </li>
          ))}
        </ol>
        {release.manifest_hash && <footer>MANIFEST SHA256 <code>{release.manifest_hash}</code></footer>}
      </section>
      {release.status === "draft" && (
        <section className="release-approval-gate">
          <div><span>GATE 01</span><h3>批准案例清单</h3><p>批准后案例顺序、记录哈希和发布说明构成不可变清单。</p></div>
          {canPublish ? <>
            <label><input type="checkbox" checked={approveConfirmed} onChange={(event) => setApproveConfirmed(event.target.checked)} />我已逐项核对案例内容、顺序与记录哈希</label>
            <button className="button is-quiet" type="button" disabled={!approveConfirmed || approveMutation.isPending} onClick={() => approveMutation.mutate()}>{approveMutation.isPending ? "批准中" : "批准发布清单"}</button>
          </> : <small>需要具名运营账号批准。</small>}
        </section>
      )}
      {release.status === "approved" && (
        <section className="release-publication-gate">
          <div><span>GATE 02</span><h3>发布正式数据集版本</h3><p>将按能力与语言拆分为多个冻结版本。</p></div>
          <strong>这些版本保持 `run_eligible=false`，不进入 Profile、Run 或排行榜。</strong>
          {canPublish ? <>
            <label><input type="checkbox" checked={publishConfirmed} onChange={(event) => setPublishConfirmed(event.target.checked)} />我确认本次发布不可用于评测运行，也不会影响排行榜</label>
            <button className="button is-danger" type="button" disabled={!publishConfirmed || publishMutation.isPending} onClick={() => publishMutation.mutate()}>{publishMutation.isPending ? "发布中" : "正式发布不可运行版本"}</button>
          </> : <small>需要具名运营账号发布。</small>}
        </section>
      )}
      {release.status === "published" && (
        <section className="released-version-board">
          <header><div><span>FORMAL DATASET VERSIONS</span><h3>已生成冻结版本</h3></div><b>RUN ISOLATED</b></header>
          <div>
            {release.dataset_versions.map((version) => (
              <article key={version.id}>
                <strong>{version.benchmark_name}</strong>
                <span>{abilityLabel(version.ability)} · {version.language.toUpperCase()} · V{version.version}</span>
                <dl><div><dt>QUESTIONS</dt><dd>{version.question_count}</dd></div><div><dt>RUN ELIGIBLE</dt><dd>{version.run_eligible ? "YES" : "NO"}</dd></div></dl>
                <code>{version.content_hash}</code>
              </article>
            ))}
          </div>
          <footer>未创建 Run Profile、评测任务、LeaderboardSnapshot 或 LeaderboardResult。</footer>
        </section>
      )}
    </article>
  );
}

export function DatasetReleasesPage({ userId, canPublish }: { userId: string; canPublish: boolean }) {
  const { releaseId } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [pageError, setPageError] = useState<string | null>(null);
  const releasesQuery = useQuery({ queryKey: releasesKey(userId), queryFn: fetchDatasetReleases });
  const casesQuery = useQuery({ queryKey: casesKey(userId), queryFn: fetchPublishedEvaluationCases });
  const releaseQuery = useQuery({
    queryKey: releaseKey(userId, releaseId ?? "new"),
    queryFn: () => fetchDatasetRelease(releaseId!),
    enabled: Boolean(releaseId)
  });
  const publishedCases = (casesQuery.data?.items ?? []).filter((item) => item.status === "published");

  function updateCaches(updated: DatasetRelease) {
    setPageError(null);
    queryClient.setQueryData(releaseKey(userId, updated.id), updated);
    queryClient.setQueryData<DatasetReleaseList>(releasesKey(userId), (current) => {
      if (!current) return { items: [updated] };
      const exists = current.items.some((item) => item.id === updated.id);
      return { items: exists ? current.items.map((item) => item.id === updated.id ? updated : item) : [updated, ...current.items] };
    });
  }

  function created(release: DatasetRelease) {
    updateCaches(release);
    navigate(`/dataset-releases/${release.id}`);
  }

  async function refetchConflict() {
    if (!releaseId) return;
    setPageError("发布批次已在其他位置更新，正在读取最新版本。");
    await Promise.all([
      queryClient.refetchQueries({ queryKey: releaseKey(userId, releaseId), exact: true }),
      queryClient.refetchQueries({ queryKey: releasesKey(userId), exact: true })
    ]);
    setPageError("发布批次已更新，已读取最新版本，请检查当前状态。");
  }

  return (
    <main className="workspace dataset-releases-page">
      <header className="dataset-releases-header">
        <div><p className="eyebrow">CONTROLLED DATASET PUBLICATION</p><h1>正式数据集发布</h1><p>将已发布精选案例组成可追溯的数据集版本，并在明确批准前后保持评测链路隔离。</p></div>
        <Link className="back-link" to="/evaluation-cases">← 精选评测案例</Link>
      </header>
      {pageError && <div className="result-banner is-error" role="alert">{pageError}</div>}
      {releasesQuery.error && <div className="result-banner is-error" role="alert">发布历史读取失败：{releasesQuery.error.message}</div>}
      <div className="dataset-releases-layout">
        <ReleaseHistory releases={releasesQuery.data?.items ?? []} activeId={releaseId} />
        {!releaseId ? (
          <CreateRelease cases={publishedCases} loading={casesQuery.isPending} error={casesQuery.error?.message ?? null} canPublish={canPublish} onCreated={created} />
        ) : releaseQuery.isPending ? (
          <div className="workspace-detail-state"><span className="status-dot" />正在装载发布清单</div>
        ) : releaseQuery.error || !releaseQuery.data ? (
          <div className="workspace-detail-state is-error"><strong>无法读取发布批次</strong><span>{releaseQuery.error?.message ?? "发布批次不存在"}</span></div>
        ) : (
          <ReleaseDetail key={`${releaseQuery.data.id}-${releaseQuery.data.revision}`} release={releaseQuery.data} canPublish={canPublish} onUpdated={updateCaches} onRevisionConflict={() => void refetchConflict()} />
        )}
      </div>
    </main>
  );
}
