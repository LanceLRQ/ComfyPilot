import { useRef, useState } from "react";
import { z } from "zod";
import { randomUUID } from "./uuid";
import type { Version } from "./Versions";
import { useWorkspace } from "./Workspace";

const resultSchema = z.object({
  error: z.object({ code: z.string(), message: z.string() }).optional(),
});

export function Decisions({ version }: { version: Version }) {
  const { snapshot, stale } = useWorkspace();
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const pending = useRef<{
    version_id: string;
    decision: "adopt" | "reject";
    command_id: string;
    expected_revision: number;
  } | null>(null);
  const adopted = snapshot?.data.adoptions.some((item) => item.version_id === version.id);
  async function decide(decision: "adopt" | "reject") {
    if (!snapshot) return;
    setBusy(true);
    if (
      !pending.current ||
      pending.current.version_id !== version.id ||
      pending.current.decision !== decision
    ) {
      pending.current = {
        version_id: version.id,
        decision,
        command_id: randomUUID(),
        expected_revision: snapshot.data.revision,
      };
    }
    try {
      const response = await fetch("/api/execution/command", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-ComfyPilot-Instance": snapshot.instance_id,
        },
        signal: AbortSignal.timeout(8000),
        body: JSON.stringify({
          ...pending.current,
          action: "record_user_decision",
          source: "web_user",
          user_statement:
            decision === "adopt"
              ? "用户在网页点击通过并采用当前候选。"
              : "用户在网页明确拒绝当前候选。",
        }),
      });
      const result = resultSchema.parse(await response.json());
      if (!response.ok) {
        pending.current = null;
        throw new Error(result.error?.message ?? "决定未保存，请核对后重试。");
      }
      pending.current = null;
      setMessage(
        decision === "adopt"
          ? "用户采用已保存。后续失败候选不会替换它。"
          : "拒绝已保存；其他已采用版本保持原引用。",
      );
    } catch (error) {
      setMessage(
        error instanceof Error
          ? `${error.message} 当前选择与未确认命令已保留。`
          : "回执未收到，可用原命令重试。",
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="review-feedback" aria-labelledby="decision-title">
      <div className="section-heading">
        <h3 id="decision-title">审片决定</h3>
        <span>{adopted ? "用户已采用" : "候选 · 尚未采用"}</span>
      </div>
      <p className="settings-note">技术检查和 Agent 评价供参考；正式交付只使用你明确采用的版本。</p>
      <div className="resource-actions">
        <button
          type="button"
          disabled={busy || stale || adopted || version.checks.status !== "passed"}
          onClick={() => void decide("adopt")}
        >
          通过并采用当前候选
        </button>
        <button type="button" disabled={busy || stale} onClick={() => void decide("reject")}>
          拒绝当前候选
        </button>
      </div>
      {message && <p role="status">{message}</p>}
      {snapshot?.data.decisions
        .filter((item) => item.version_id === version.id)
        .slice(0, 5)
        .map((item) => (
          <p key={item.id} className="settings-note">
            {item.decision === "adopt" ? "用户采用" : "用户拒绝"} · {item.source} ·{" "}
            {item.user_statement}
          </p>
        ))}
      {snapshot?.data.evaluations
        .filter((item) => item.version_id === version.id)
        .map((item) => (
          <p key={item.id} className="settings-note">
            Agent 技术评价：{item.text}
          </p>
        ))}
    </section>
  );
}

export function Delivery() {
  const { snapshot } = useWorkspace();
  const adoptions = snapshot?.data.adoptions ?? [];
  return (
    <section className="review-feedback">
      <div className="section-heading">
        <h3>采用与交付清单</h3>
        <span>{adoptions.length} 段</span>
      </div>
      {!adoptions.length ? (
        <p className="settings-note">尚未采用版本。生成成功或技术合格不会自动进入交付。</p>
      ) : (
        <>
          <a href="/api/review/delivery?download=true" download>
            下载采用与来源清单
          </a>
          <ul className="feedback-items">
            {adoptions.map((item) => (
              <li key={`${item.task_id}:${item.segment_id}:${item.slot}`}>
                <strong>
                  片段 {item.segment_id.slice(0, 8)} · 版本 {item.version_id.slice(0, 8)}
                </strong>
                <a href={`/api/versions/${item.version_id}/content?variant=original`} download>
                  下载已采用原件
                </a>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
