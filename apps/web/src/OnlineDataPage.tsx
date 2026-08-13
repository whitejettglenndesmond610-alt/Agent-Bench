import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

import {
  BadPatternSeverity,
  batchReviewOnlineDataRecords,
  createOnlineDataBatch,
  fetchOnlineDataBatches,
  fetchOnlineDataRecord,
  fetchOnlineDataRecords,
  fetchOnlineDataSummary,
  OnlineDataBatchInput,
  OnlineDataEnvironment,
  OnlineDataRecordStatus
} from "./api";
import { Button, FormField, Input, LoadingState, Select, Textarea } from "./components";
import { randomId } from "./randomId";

const environmentLabels: Record<OnlineDataEnvironment, string> = {
  production: "生产",
  staging: "预发",
  replay: "历史回放"
};
const statusLabels: Record<OnlineDataRecordStatus, string> = {
  pending: "待处理",
  retained: "已保留",
  discarded: "已丢弃"
};
const severityLabels: Record<BadPatternSeverity, string> = {
  critical: "严重",
  high: "高危",
  medium: "中危",
  low: "低危"
};
const sampleRecords = JSON.stringify([
  {
    external_id: "trace-20260814-001",
    agent_name: "support-agent",
    agent_version: "2026.08",
    prompt: "请总结这次客户咨询。",
    answer: "已根据上下文生成摘要。",
    occurred_at: new Date().toISOString(),
    trace: [
      { kind: "model", title: "模型回答", status: "completed", detail: "线上 Agent 最终输出", occurred_at: new Date().toISOString(), latency_ms: 842 }
    ],
    metadata: { channel: "web", tenant: "demo" }
  }
], null, 2);

function formatDate(value: string | null): string {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit"
  }).format(new Date(value));
}

function parseRecords(value: string): OnlineDataBatchInput["records"] {
  const parsed: unknown = JSON.parse(value);
  const records = Array.isArray(parsed)
    ? parsed
    : typeof parsed === "object" && parsed !== null && "records" in parsed
      ? (parsed as { records: unknown }).records
      : null;
  if (!Array.isArray(records) || records.length === 0) {
    throw new Error("请输入非空 records JSON 数组");
  }
  return records as OnlineDataBatchInput["records"];
}

export function OnlineDataPage() {
  const queryClient = useQueryClient();
  const [recordStatus, setRecordStatus] = useState<OnlineDataRecordStatus | "">("pending");
  const [environment, setEnvironment] = useState<OnlineDataEnvironment | "">("");
  const [riskSeverity, setRiskSeverity] = useState<BadPatternSeverity | "">("");
  const [searchDraft, setSearchDraft] = useState("");
  const [search, setSearch] = useState("");
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [activeRecordId, setActiveRecordId] = useState<string | null>(null);
  const [reviewNote, setReviewNote] = useState("");
  const [showImporter, setShowImporter] = useState(false);
  const [sourceName, setSourceName] = useState("agent-gateway");
  const [importEnvironment, setImportEnvironment] = useState<OnlineDataEnvironment>("production");
  const [externalBatchId, setExternalBatchId] = useState(`batch-${new Date().toISOString().slice(0, 10)}`);
  const [recordsJson, setRecordsJson] = useState(sampleRecords);
  const [importError, setImportError] = useState<string | null>(null);
  const lastImportFingerprint = useRef<string | null>(null);
  const lastImportIdempotencyKey = useRef<string | null>(null);

  const filters = useMemo(() => ({
    status: recordStatus || undefined,
    environment: environment || undefined,
    risk_severity: riskSeverity || undefined,
    search: search || undefined,
    limit: 40
  }), [environment, recordStatus, riskSeverity, search]);
  const summaryQuery = useQuery({ queryKey: ["online-data-summary"], queryFn: fetchOnlineDataSummary });
  const batchesQuery = useQuery({ queryKey: ["online-data-batches"], queryFn: () => fetchOnlineDataBatches(8) });
  const recordsQuery = useInfiniteQuery({
    queryKey: ["online-data-records", filters],
    queryFn: ({ pageParam }) => fetchOnlineDataRecords({ ...filters, cursor: pageParam || undefined }),
    initialPageParam: "",
    getNextPageParam: (page) => page.next_cursor ?? undefined
  });
  const records = recordsQuery.data?.pages.flatMap((page) => page.items) ?? [];
  const detailQuery = useQuery({
    queryKey: ["online-data-record", activeRecordId],
    queryFn: () => fetchOnlineDataRecord(activeRecordId as string),
    enabled: Boolean(activeRecordId)
  });

  async function refreshData() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["online-data-summary"] }),
      queryClient.invalidateQueries({ queryKey: ["online-data-batches"] }),
      queryClient.invalidateQueries({ queryKey: ["online-data-records"] }),
      queryClient.invalidateQueries({ queryKey: ["online-data-record"] })
    ]);
  }
  const importMutation = useMutation({
    mutationFn: createOnlineDataBatch,
    onSuccess: async () => {
      setImportError(null);
      setExternalBatchId(`batch-${Date.now()}`);
      lastImportFingerprint.current = null;
      lastImportIdempotencyKey.current = null;
      await refreshData();
    }
  });
  const reviewMutation = useMutation({
    mutationFn: (status: "retained" | "discarded") => batchReviewOnlineDataRecords({
      record_ids: Array.from(selectedIds),
      status,
      note: reviewNote.trim() || undefined
    }),
    onSuccess: async () => {
      setSelectedIds(new Set());
      setReviewNote("");
      await refreshData();
    }
  });

  function submitImport(event: FormEvent) {
    event.preventDefault();
    try {
      const records = parseRecords(recordsJson);
      setImportError(null);
      const submission = {
        source_name: sourceName.trim(),
        environment: importEnvironment,
        external_batch_id: externalBatchId.trim(),
        records
      };
      const fingerprint = JSON.stringify(submission);
      if (lastImportFingerprint.current !== fingerprint) {
        lastImportFingerprint.current = fingerprint;
        lastImportIdempotencyKey.current = randomId();
      }
      importMutation.mutate({
        ...submission,
        idempotency_key: lastImportIdempotencyKey.current as string
      });
    } catch (error) {
      setImportError(error instanceof Error ? error.message : "JSON 解析失败");
    }
  }

  function toggleSelection(recordId: string) {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(recordId)) next.delete(recordId);
      else next.add(recordId);
      return next;
    });
  }

  const summary = summaryQuery.data;
  const detail = detailQuery.data;
  const mutationError = importMutation.error ?? reviewMutation.error;

  return (
    <main className="workspace online-data-workspace">
      <header className="online-data-header">
        <div>
          <p className="eyebrow">PRODUCTION FEEDBACK PIPELINE</p>
          <h1>线上数据回流，<br />进入可追溯的质检闭环。</h1>
          <p>按来源幂等接收 Agent 轨迹，自动预扫 Bad Pattern，再由人工保留为评测资产或安全丢弃。</p>
        </div>
        <div className="online-data-header-actions">
          <Link className="button is-ghost" to="/quality">返回质量闭环</Link>
          <Button type="button" onClick={() => setShowImporter((current) => !current)}>
            {showImporter ? "收起导入" : "导入回流批次"}
          </Button>
        </div>
      </header>

      <section className="online-data-kpis" aria-label="线上回流指标">
        <article><span>回流批次</span><strong>{summary?.total_batches ?? "—"}</strong><small>{summary?.latest_received_at ? `最近 ${formatDate(summary.latest_received_at)}` : "等待首个批次"}</small></article>
        <article><span>累计样本</span><strong>{summary?.total_records ?? "—"}</strong><small>跨来源去重后入库</small></article>
        <article className="is-pending"><span>待处理</span><strong>{summary?.pending_records ?? "—"}</strong><small>等待人工数据判定</small></article>
        <article className="is-risk"><span>风险样本</span><strong>{summary?.risky_records ?? "—"}</strong><small>命中 Bad Pattern</small></article>
        <article><span>已保留</span><strong>{summary?.retained_records ?? "—"}</strong><small>可进入后续评测资产</small></article>
        <article><span>已丢弃</span><strong>{summary?.discarded_records ?? "—"}</strong><small>不进入数据集</small></article>
      </section>

      {showImporter && <form className="online-data-importer" onSubmit={submitImport}>
        <header><div><span>BATCH INGEST</span><strong>导入 JSON 回流批次</strong></div><small>单批最多 500 条；source + external_id 全局去重</small></header>
        <div className="online-data-import-fields">
          <FormField label="来源标识" htmlFor="online-source" required><Input id="online-source" value={sourceName} onChange={(event) => setSourceName(event.target.value)} required /></FormField>
          <FormField label="环境" htmlFor="online-environment" required><Select id="online-environment" value={importEnvironment} onChange={(event) => setImportEnvironment(event.target.value as OnlineDataEnvironment)}><option value="production">生产</option><option value="staging">预发</option><option value="replay">历史回放</option></Select></FormField>
          <FormField label="来源批次编号" htmlFor="online-batch" required><Input id="online-batch" value={externalBatchId} onChange={(event) => setExternalBatchId(event.target.value)} required /></FormField>
          <FormField label="records JSON" htmlFor="online-records" hint="支持数组或 { records: [...] }" required><Textarea id="online-records" rows={12} value={recordsJson} onChange={(event) => setRecordsJson(event.target.value)} spellCheck={false} required /></FormField>
        </div>
        {(importError || importMutation.error) && <p className="online-data-error">{importError ?? importMutation.error?.message}</p>}
        {importMutation.data && <p className="online-data-success">批次已接收：{importMutation.data.accepted_count} 条入库，{importMutation.data.duplicate_count} 条重复跳过。</p>}
        <footer><Button type="submit" disabled={importMutation.isPending}>{importMutation.isPending ? "正在导入" : "校验并导入批次"}</Button></footer>
      </form>}

      <section className="online-data-batches" aria-label="最近回流批次">
        <header><div><span>RECENT BATCHES</span><strong>最近回流批次</strong></div><small>显示最近 8 个接收结果</small></header>
        {batchesQuery.isPending ? <LoadingState label="正在读取回流批次" compact /> :
          <div>{batchesQuery.data?.items.map((batch) => <article key={batch.id}>
            <div><span className={`online-environment is-${batch.environment}`}>{environmentLabels[batch.environment]}</span><time>{formatDate(batch.created_at)}</time></div>
            <strong>{batch.source_name}</strong><code>{batch.external_batch_id}</code>
            <footer><span>{batch.accepted_count}/{batch.received_count} 入库</span>{batch.duplicate_count > 0 && <b>{batch.duplicate_count} 重复</b>}</footer>
          </article>)}</div>}
      </section>

      <section className="online-data-filter-bar">
        <form onSubmit={(event) => { event.preventDefault(); setSearch(searchDraft.trim()); }}>
          <Input aria-label="搜索线上样本" value={searchDraft} onChange={(event) => setSearchDraft(event.target.value)} placeholder="搜索 Agent 或来源记录编号" />
          <Button type="submit" className="is-ghost">搜索</Button>
        </form>
        <Select aria-label="样本状态" value={recordStatus} onChange={(event) => { setRecordStatus(event.target.value as OnlineDataRecordStatus | ""); setSelectedIds(new Set()); }}><option value="">全部状态</option><option value="pending">待处理</option><option value="retained">已保留</option><option value="discarded">已丢弃</option></Select>
        <Select aria-label="回流环境" value={environment} onChange={(event) => setEnvironment(event.target.value as OnlineDataEnvironment | "")}><option value="">全部环境</option><option value="production">生产</option><option value="staging">预发</option><option value="replay">历史回放</option></Select>
        <Select aria-label="风险等级" value={riskSeverity} onChange={(event) => setRiskSeverity(event.target.value as BadPatternSeverity | "")}><option value="">全部风险</option><option value="critical">严重</option><option value="high">高危</option><option value="medium">中危</option><option value="low">低危</option></Select>
      </section>

      <section className={`online-data-batch-bar ${selectedIds.size ? "is-active" : ""}`} aria-label="批量数据判定">
        <strong>已选 {selectedIds.size} 条</strong>
        <Input aria-label="批量判定说明" value={reviewNote} onChange={(event) => setReviewNote(event.target.value)} placeholder="判定依据（选填）" />
        <Button type="button" disabled={!selectedIds.size || reviewMutation.isPending} onClick={() => reviewMutation.mutate("retained")}>保留为数据资产</Button>
        <Button type="button" className="is-ghost" disabled={!selectedIds.size || reviewMutation.isPending} onClick={() => reviewMutation.mutate("discarded")}>丢弃样本</Button>
        {reviewMutation.error && <small>{reviewMutation.error.message}</small>}
      </section>

      <section className="online-data-layout">
        <aside className="online-data-list" aria-label="线上回流样本列表">
          <header><span>CURSOR QUEUE</span><strong>回流样本</strong><small>按发生时间倒序 · 稳定游标分页</small></header>
          {recordsQuery.isPending ? <LoadingState label="正在读取回流样本" compact /> : recordsQuery.error ? <div className="online-data-state is-error">{recordsQuery.error.message}</div> : records.length ? <>
            {records.map((record) => <div key={record.id} className={`online-data-row ${record.id === activeRecordId ? "is-active" : ""}`}>
              <label><input type="checkbox" aria-label={`选择 ${record.external_id}`} checked={selectedIds.has(record.id)} onChange={() => toggleSelection(record.id)} /><span /></label>
              <button type="button" onClick={() => setActiveRecordId(record.id)}>
                <div><span className={`online-data-status is-${record.status}`}>{statusLabels[record.status]}</span>{record.risk_severity && <span className={`quality-severity is-${record.risk_severity}`}>{severityLabels[record.risk_severity]}</span>}<time>{formatDate(record.occurred_at)}</time></div>
                <strong>{record.agent_name}{record.agent_version ? ` · ${record.agent_version}` : ""}</strong>
                <code>{record.source_name} / {record.external_id}</code>
                <p>{record.answer_preview}</p>
              </button>
            </div>)}
            {recordsQuery.hasNextPage && <Button type="button" className="is-ghost online-data-more" disabled={recordsQuery.isFetchingNextPage} onClick={() => recordsQuery.fetchNextPage()}>{recordsQuery.isFetchingNextPage ? "正在加载" : "加载更早样本"}</Button>}
          </> : <div className="online-data-state"><strong>当前筛选没有样本</strong><span>导入批次后，记录会在此进入预扫和人工判定。</span></div>}
        </aside>

        <article className="online-data-detail">
          {!activeRecordId ? <div className="online-data-empty"><span>TRACE INSPECTOR</span><h2>选择一条回流样本</h2><p>查看完整输入输出、自动检测依据、Agent 轨迹与安全元数据。</p></div> : detailQuery.isPending ? <LoadingState label="正在读取样本详情" /> : detailQuery.error || !detail ? <div className="online-data-state is-error">{detailQuery.error?.message ?? "详情读取失败"}</div> : <>
            <header><div><span className={`online-data-status is-${detail.status}`}>{statusLabels[detail.status]}</span>{detail.risk_severity && <span className={`quality-severity is-${detail.risk_severity}`}>{severityLabels[detail.risk_severity]}</span>}<h2>{detail.agent_name}</h2><p>{detail.source_name} / {detail.external_id}</p></div><time>{formatDate(detail.occurred_at)}</time></header>
            <section className="online-data-evidence"><article><span>PROMPT</span><pre>{detail.prompt}</pre></article><article><span>ANSWER</span><pre>{detail.answer}</pre></article></section>
            <section className="online-data-matches"><header><span>AUTOMATED PRE-SCAN</span><strong>Bad Pattern 命中</strong></header>{detail.matched_rules.length ? <div>{detail.matched_rules.map((rule) => <article key={rule.key}><span className={`quality-severity is-${rule.severity}`}>{severityLabels[rule.severity]}</span><strong>{rule.name}</strong><small>{rule.category} · {rule.match_count} 次</small><p>{rule.snippet}</p></article>)}</div> : <p>未命中当前启用的底线规则。</p>}</section>
            <section className="online-data-trace"><header><span>AGENT TRACE</span><strong>{detail.trace.length} 个轨迹事件</strong></header>{detail.trace.length ? <ol>{detail.trace.map((event, index) => <li key={`${event.kind}-${index}`}><i /><div><strong>{event.title ?? event.kind}</strong><span>{event.status ?? "recorded"}{event.latency_ms === null ? "" : ` · ${event.latency_ms} ms`}</span><p>{event.detail ?? "无详情"}</p></div><time>{formatDate(event.occurred_at)}</time></li>)}</ol> : <p>来源未提供轨迹事件。</p>}</section>
            <section className="online-data-metadata"><header><span>SAFE METADATA</span><strong>来源元数据</strong></header><pre>{JSON.stringify(detail.metadata, null, 2)}</pre><small>CONTENT SHA-256 · {detail.content_hash}</small></section>
          </>}
        </article>
      </section>
      {mutationError && !showImporter && <p className="online-data-error">{mutationError.message}</p>}
    </main>
  );
}
