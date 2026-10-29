import { useEffect, useRef, useState } from "react";
import { z } from "zod";
import { runStates } from "./ProductionActivity";
import { SidePanel } from "./SidePanel";
import { useWorkspace } from "./Workspace";

const evidenceKinds = { graph: "最终执行图", history: "外部回执历史", checks: "媒体检查" };
type EvidenceKind = keyof typeof evidenceKinds;
const evidenceReply = z.object({
  data: z.object({ evidence: z.unknown() }).optional(),
  error: z.object({ message: z.string() }).optional(),
});

function RunDetail({ id }: { id: string }) {
  const { snapshot, stale } = useWorkspace();
  const run = snapshot?.data.runs.find((item) => item.id === id);
  const plan = snapshot?.data.plans.find((item) => item.id === run?.plan_id);
  const task = snapshot?.data.tasks.find((item) => item.id === plan?.task_id);
  const collection = snapshot?.data.collections.find((item) => item.run_id === id);
  const versions = snapshot?.data.versions.filter((item) => item.run_id === id) ?? [];
  const [evidence, setEvidence] = useState<{ kind: EvidenceKind; text: string } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  async function readEvidence(kind: EvidenceKind) {
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setError("");
    setEvidence(null);
    try {
      const response = await fetch(`/api/execution/runs/${id}/evidence?kind=${kind}`, {
        cache: "no-store",
        signal: AbortSignal.any([controller.signal, AbortSignal.timeout(8000)]),
      });
      const result = evidenceReply.parse(await response.json());
      if (!response.ok || !result.data)
        throw new Error(result.error?.message ?? "证据暂不可读，可稍后重试。");
      if (!controller.signal.aborted)
        setEvidence({ kind, text: JSON.stringify(result.data.evidence, null, 2) });
    } catch (failure) {
      if (!controller.signal.aborted)
        setError(failure instanceof Error ? failure.message : "证据读取失败，可重试。");
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }
  if (!run) return <p role="status">当前快照中没有此运行，请等待状态恢复或回到任务列表核对。</p>;
  return (
    <>
      {stale && <p role="alert">连接中断，以下为最后快照，状态可能已过期。</p>}
      <p className="run-status">{runStates[run.status] ?? run.status}</p>
      <dl className="run-fields">
        <div>
          <dt>运行</dt>
          <dd>{run.id}</dd>
        </div>
        <div>
          <dt>任务</dt>
          <dd>{task?.goal ?? "当前快照未提供"}</dd>
        </div>
        <div>
          <dt>方案</dt>
          <dd>
            {run.plan_id} · {plan?.status ?? "未知"}
          </dd>
        </div>
        <div>
          <dt>步骤 / 片段</dt>
          <dd>
            {run.action_index + 1} / {run.document.step?.segment_id ?? "未声明"}
          </dd>
        </div>
        <div>
          <dt>冻结目标</dt>
          <dd>{run.document.step?.target_id ?? "尚未声明"}</dd>
        </div>
        <div>
          <dt>提交意图</dt>
          <dd>
            {run.has_intent ? "已保存" : "尚无意图"} ·{" "}
            {run.started_at
              ? new Date(run.started_at).toLocaleString("zh-CN", { hour12: false })
              : "时间未提供"}
          </dd>
        </div>
        <div>
          <dt>外部回执</dt>
          <dd>
            {run.document.receipt?.prompt_id ??
              (snapshot?.data.local_control ? "尚无回执" : "请在本机查看")}
          </dd>
        </div>
        <div>
          <dt>收片阶段</dt>
          <dd>{collection?.phase ?? run.document.collection?.phase ?? "当前快照未提供"}</dd>
        </div>
        {collection?.bytes_done !== undefined && (
          <div>
            <dt>已收 / 总字节</dt>
            <dd>
              {collection.bytes_done} / {collection.bytes_total ?? "未知"}
            </dd>
          </div>
        )}
        {run.document.dispatch_blocked && (
          <div>
            <dt>派发受阻原因</dt>
            <dd>{run.document.dispatch_blocked.code}</dd>
          </div>
        )}
        {collection?.error && (
          <div>
            <dt>收片错误</dt>
            <dd>{collection.error.message}</dd>
          </div>
        )}
      </dl>
      <h3>此运行的候选（{versions.length}）</h3>
      {!versions.length && (
        <p className="settings-note">尚无候选，节点完成或收到回执不代表已可审。</p>
      )}
      <ul className="feedback-items">
        {versions.map((version) => (
          <li key={version.id}>
            <span>
              {version.id} · {version.slot}
            </span>
            <p>
              {version.ready ? "可审" : "尚不可审"} · 检查 {version.checks.status} ·{" "}
              {snapshot?.data.adoptions.some((item) => item.version_id === version.id)
                ? "用户已采用"
                : "未采用"}
            </p>
          </li>
        ))}
      </ul>
      <h3>运行证据</h3>
      {snapshot?.data.local_control ? (
        <>
          <p className="settings-note">
            按需只读，不触发生成、恢复、取消或额度变更。尚未产生的证据可能暂不可用。
          </p>
          <div className="resource-actions">
            {Object.entries(evidenceKinds).map(([kind, label]) => (
              <button
                key={kind}
                type="button"
                aria-disabled={loading || stale}
                onClick={() => {
                  if (!loading && !stale) void readEvidence(kind as EvidenceKind);
                }}
              >
                {label}
              </button>
            ))}
          </div>
          {loading && <p role="status">正在读取证据…</p>}
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
          {evidence && (
            <section aria-label={evidenceKinds[evidence.kind]}>
              <h4>{evidenceKinds[evidence.kind]}</h4>
              <pre className="resource-code">{evidence.text}</pre>
            </section>
          )}
        </>
      ) : (
        <p className="settings-note">
          局域网仅显示脱敏运行摘要，完整执行图、历史与检查请在本机查看。
        </p>
      )}
    </>
  );
}

export function RunDrawer({ id, onClose }: { id: string | null; onClose: () => void }) {
  return (
    <SidePanel id="run-panel" title="运行详情" open={id !== null} onClose={onClose}>
      {id && <RunDetail key={id} id={id} />}
    </SidePanel>
  );
}
