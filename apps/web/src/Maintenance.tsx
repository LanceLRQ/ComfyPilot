import { useState } from "react";
import { z } from "zod";
import { useWorkspace } from "./Workspace";

const planSchema = z.object({
  id: z.string().optional(),
  estimated_bytes: z.number(),
  reference_revision: z.number(),
  items: z.array(
    z.object({
      replica_id: z.string(),
      role: z.string(),
      relative_path: z.string(),
      size: z.number(),
      eligible: z.boolean(),
      protection_reasons: z.array(z.string()),
    }),
  ),
});
const responseSchema = z.object({
  data: planSchema.optional(),
  error: z.object({ message: z.string() }).optional(),
});
const receiptSchema = z.object({
  data: z
    .object({
      command_id: z.string(),
      deleted_bytes: z.number(),
      items: z.array(z.object({ replica_id: z.string(), status: z.string() })),
    })
    .optional(),
  error: z.object({ message: z.string() }).optional(),
});
const reasons: Record<string, string> = {
  active_run: "运行仍在进行",
  plan_reference: "仍被待执行方案使用",
  pending_review: "等待审片",
  unresolved_feedback: "反馈尚未确认",
  reconciliation_required: "运行结果待核对",
  unknown_reference: "引用来源未确认",
  ownership_unknown: "归属未确认",
  uncollected_output: "结果尚未收集",
  unverified_original: "本地原件未通过检查",
  original_missing: "没有本地原件",
  original_changed: "本地原件发生变化",
  fingerprint_changed: "执行端文件发生变化",
};

export function Maintenance() {
  const { snapshot, stale } = useWorkspace();
  const [target, setTarget] = useState("");
  const [instruction, setInstruction] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [preview, setPreview] = useState<z.infer<typeof planSchema> | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [command, setCommand] = useState<string | null>(null);
  const [receipt, setReceipt] = useState<z.infer<typeof receiptSchema>["data"]>();
  async function inspect(save: boolean) {
    if (!snapshot || !target) return;
    setBusy(true);
    setMessage("");
    try {
      const response = await fetch(`/api/cleanup/${save ? "prepare" : "inspect"}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-ComfyPilot-Instance": snapshot.instance_id,
        },
        body: JSON.stringify({
          target_id: target,
          ...(save ? { user_instruction: instruction, replica_ids: selected } : {}),
        }),
      });
      const result = responseSchema.parse(await response.json());
      if (!response.ok || !result.data)
        throw new Error(result.error?.message ?? "无法取得清理预览。");
      setPreview(result.data);
      setCommand(null);
      setReceipt(undefined);
      if (!save) setSelected([]);
      setMessage(save ? "已保存清理预览，尚未删除文件。" : "盘点完成，文件保持原样。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "清理预览暂不可用。");
    } finally {
      setBusy(false);
    }
  }
  async function execute(retry = false) {
    if (!snapshot || !preview?.id) return;
    const id = retry || !command ? crypto.randomUUID() : command;
    setCommand(id);
    setBusy(true);
    try {
      const response = await fetch("/api/cleanup/execute", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-ComfyPilot-Instance": snapshot.instance_id,
        },
        body: JSON.stringify({ plan_id: preview.id, command_id: id }),
      });
      const result = receiptSchema.parse(await response.json());
      if (!response.ok || !result.data) throw new Error(result.error?.message ?? "清理执行失败。");
      setReceipt(result.data);
      setMessage("清理回执已保存。原件与审片记录保留。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "请保留命令 ID 后重试获取回执。");
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="workspace settings" aria-labelledby="maintenance-title">
      <div className="section-heading">
        <h2 id="maintenance-title">素材维护</h2>
        <span>STORAGE</span>
      </div>
      <p className="settings-note">
        仅检查本项目在所选 ComfyUI
        的受管副本。待审、运行中和仍被方案使用的素材受到保护；本地作品保留。
      </p>
      <label>
        执行端
        <select
          aria-label="维护执行端"
          value={target}
          onChange={(event) => {
            setTarget(event.target.value);
            setPreview(null);
            setSelected([]);
            setReceipt(undefined);
          }}
        >
          <option value="">请选择执行端</option>
          {snapshot?.data.monitoring.targets.map((item) => (
            <option key={item.target_id} value={item.target_id}>
              {item.target_id}
            </option>
          ))}
        </select>
      </label>
      <label>
        清理范围说明
        <input
          aria-label="清理范围说明"
          value={instruction}
          onChange={(event) => {
            setInstruction(event.target.value);
            setPreview((previous) => (previous ? { ...previous, id: undefined } : previous));
          }}
          placeholder="填写用途，实际范围以下方勾选项为准"
        />
      </label>
      <div className="settings-actions">
        <button
          type="button"
          disabled={busy || stale || !target}
          onClick={() => void inspect(false)}
        >
          盘点素材
        </button>
        <button
          type="button"
          disabled={busy || stale || !target || !instruction.trim() || !selected.length}
          onClick={() => void inspect(true)}
        >
          保存清理预览
        </button>
        <button
          type="button"
          disabled={busy || stale || !preview?.id || !!receipt}
          onClick={() => void execute()}
        >
          执行此清单
        </button>
        {receipt?.items.some((item) => item.status === "failed") && (
          <button
            type="button"
            disabled={busy || stale || !preview?.id}
            onClick={() => void execute(true)}
          >
            按原清单重试
          </button>
        )}
      </div>
      {message && <p role="status">{message}</p>}
      {command && (
        <p>
          清理命令：<code>{command}</code>
        </p>
      )}
      {receipt && (
        <div>
          <p>
            已删除文件大小估算：{receipt.deleted_bytes.toLocaleString()}{" "}
            字节；磁盘空闲变化见逐项持久回执。
          </p>
          <ul>
            {receipt.items.map((item) => (
              <li key={item.replica_id}>
                <code>{item.replica_id}</code> · {item.status}
              </li>
            ))}
          </ul>
        </div>
      )}
      {preview && (
        <div>
          <p>
            预计可删除副本：{preview.estimated_bytes.toLocaleString()} 字节 · 引用修订{" "}
            {preview.reference_revision}
          </p>
          {preview.id && (
            <p>
              清理计划：<code>{preview.id}</code>
            </p>
          )}
          <ul>
            {preview.items.map((item) => (
              <li key={item.replica_id}>
                <input
                  type="checkbox"
                  aria-label={`选择副本 ${item.replica_id}`}
                  disabled={!item.eligible || busy}
                  checked={selected.includes(item.replica_id)}
                  onChange={(event) => {
                    setSelected((previous) =>
                      event.target.checked
                        ? [...previous, item.replica_id]
                        : previous.filter((id) => id !== item.replica_id),
                    );
                    setPreview((previous) =>
                      previous ? { ...previous, id: undefined } : previous,
                    );
                  }}
                />
                <code>{item.relative_path}</code> · {item.size.toLocaleString()} 字节 ·{" "}
                {item.eligible
                  ? "可清理"
                  : `保护：${item.protection_reasons.map((reason) => reasons[reason] ?? "文件状态需复核").join("、")}`}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
