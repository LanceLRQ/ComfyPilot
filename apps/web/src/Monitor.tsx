import { useState } from "react";
import type { z } from "zod";

import type { metric } from "./monitoring";
import { useWorkspace } from "./Workspace";

type Metric = z.infer<typeof metric>;
const labels = { fresh: "最新", stale: "已过期", unknown: "未知", unsupported: "不支持" };
const names: Record<string, string> = {
  ram_total: "总内存",
  ram_free: "可用内存",
  queue_running: "外部运行队列",
  queue_pending: "外部等待队列",
  comfyui_version: "ComfyUI 版本",
  gpu_utilization: "GPU 利用率",
  cpu_utilization: "CPU 利用率",
  disk_free: "可用磁盘",
  full_logs: "完整日志",
  gpu_temperature: "GPU 温度",
  os: "系统",
};

function show(item: Metric) {
  if (item.availability === "unknown" || item.availability === "unsupported")
    return labels[item.availability];
  const value = item.value;
  if (value === true) return "已连接";
  if (value === false) return "不可连接";
  if (typeof value === "number" && item.unit === "bytes")
    return `${(value / 1024 ** 3).toFixed(2)} GiB`;
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

export function Monitor() {
  const { snapshot, stale } = useWorkspace();
  const [error, setError] = useState(false);
  const [probing, setProbing] = useState<string | null>(null);
  const monitoring = snapshot?.data.monitoring;
  async function probe(id: string) {
    if (!snapshot) return;
    setProbing(id);
    try {
      const response = await fetch("/api/targets/probe", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-ComfyPilot-Instance": snapshot.instance_id,
        },
        body: JSON.stringify({ target_id: id }),
        signal: AbortSignal.timeout(4000),
      });
      if (!response.ok) throw new Error("probe_unavailable");
      setError(false);
    } catch {
      setError(true);
    } finally {
      setProbing(null);
    }
  }
  return (
    <aside className="production-monitor" aria-labelledby="monitor-title">
      <div className="section-heading">
        <h3 id="monitor-title">生产监控</h3>
        <span>每 5 秒刷新 · 15 秒过期</span>
      </div>
      <p className="settings-note">
        只显示实际采集值。外部队列不代表本项目任务，节点步数不代表整体生成进度。
      </p>
      {(error || stale) && (
        <p className="error" role="alert">
          状态读取或探测失败，保留的快照可能已过期，请等待重连。
        </p>
      )}
      <details open>
        <summary>平台与 Worker</summary>
        <p className="settings-note">
          Worker {stale ? "连接中断，快照已过期" : (monitoring?.worker.status ?? "未知")} · PID{" "}
          {monitoring?.worker.pid ?? "未知"}
          <br />
          心跳：{monitoring?.worker.last_seen_at ?? "尚无心跳"}
          <br />
          来源：项目 Worker 心跳 · 15 秒过期
        </p>
        {Object.entries(monitoring?.platform ?? {}).map(([key, value]) => (
          <p className="settings-note" key={key}>
            {key === "api" ? "平台服务" : key === "disk_free" ? "项目磁盘可用" : "项目磁盘总量"}：
            {show(value)}
            <br />
            {value.source} · {value.observed_at ?? "未采集"} · 过期：{value.expires_at ?? "未知"}
            {stale || (value.expires_at && Date.parse(value.expires_at) <= Date.now())
              ? "（已过期）"
              : ""}
          </p>
        ))}
      </details>
      <details open>
        <summary>活动运行与收片</summary>
        {snapshot?.data.runs
          .filter((run) =>
            ["dispatching", "submitted", "running", "reconciling", "collecting"].includes(
              run.status,
            ),
          )
          .map((run) => (
            <p key={run.id} className="settings-note">
              运行 {run.id.slice(0, 8)} · {run.status}
              <br />
              片段 {run.document.step?.segment_id?.slice(0, 8) ?? "未声明"} · 目标{" "}
              {run.document.step?.target_id ?? "未知"}
              <br />
              {run.started_at &&
                `自提交意图起已过 ${Math.max(0, Math.floor((Date.now() - Date.parse(run.started_at)) / 1000))} 秒`}
            </p>
          ))}
        {snapshot?.data.collections.map((item) => (
          <p className="settings-note" key={item.run_id}>
            收片 {item.run_id.slice(0, 8)} · {item.phase}
            <br />
            {item.bytes_done ?? 0} / {item.bytes_total ?? "未知"} 字节
            {item.error && ` · ${item.error.message}`}
          </p>
        ))}
        <p className="settings-note">
          模型加载未回调时，无法区分实际加载阶段；保留最近阶段与耗时，不估算整体进度或剩余时间。
        </p>
      </details>
      {monitoring?.targets.length === 0 && (
        <p className="settings-note">尚未配置执行端，请先在项目设置中添加。</p>
      )}
      {monitoring?.targets.map((item) => (
        <details className="target-editor" key={item.target_id} open>
          <summary>执行端 {item.target_id}</summary>
          <div className="section-heading">
            <h3>
              {item.target_id} · {item.enabled ? "已启用" : "已停用"}
            </h3>
            {snapshot?.data.local_control && (
              <button
                type="button"
                disabled={!item.enabled || probing !== null}
                onClick={() => void probe(item.target_id)}
              >
                {probing === item.target_id ? "探测中…" : "重新探测能力"}
              </button>
            )}
          </div>
          <p className="settings-note">
            {item.stage.value?.phase === "executing"
              ? `当前阶段已持续 ${Math.floor(item.stage_elapsed_seconds ?? 0)} 秒`
              : "阶段未知，等待执行端回调"}
            ；无节点回调时仅展示阶段与耗时。
          </p>
          <div className="settings-grid">
            {Object.entries({
              connection: item.connection,
              websocket: item.websocket,
              ...item.metrics,
              ...Object.fromEntries(
                (item.devices ?? []).flatMap((device, index) =>
                  Object.entries(device).map(([key, value]) => [`GPU ${index} ${key}`, value]),
                ),
              ),
              node_progress: item.node_progress,
              stage: item.stage,
            }).map(([key, value]) => (
              <div className="project-detail" key={key}>
                <span className="label">
                  {names[key] ??
                    {
                      connection: "HTTP 连接",
                      websocket: "事件连接",
                      node_progress: "节点实际步数",
                      stage: "最近阶段",
                    }[key] ??
                    key}
                </span>
                <span>
                  {key === "node_progress" && item.node_progress.value
                    ? `${item.node_progress.value.value} / ${item.node_progress.value.max} 步`
                    : key === "stage" && item.stage.value
                      ? `${{ executing: "执行中", execution_finished: "执行结束", execution_error: "执行报错" }[item.stage.value.phase]}${item.stage.value.node ? ` · 节点 ${item.stage.value.node}` : ""}`
                      : show(value)}
                  {stale ||
                  value.availability === "stale" ||
                  (value.expires_at && Date.parse(value.expires_at) <= Date.now())
                    ? "（已过期）"
                    : ""}
                </span>
                <details className="settings-note">
                  <summary>采集来源与时间</summary>
                  <div>{value.source}</div>
                  {value.reason && <div>{value.reason}</div>}
                  <div>{value.observed_at ?? "未采集"}</div>
                  <div>过期：{value.expires_at ?? "未知"}</div>
                </details>
              </div>
            ))}
          </div>
          <p className="settings-note">
            {Object.entries(item.capabilities)
              .map(([key, value]) => `${key}: ${value.status}（${value.reason}）`)
              .join("；")}
          </p>
        </details>
      ))}
    </aside>
  );
}
