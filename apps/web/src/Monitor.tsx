import { useEffect, useState } from "react";
import { z } from "zod";

const metric = z.object({
  value: z.unknown(),
  source: z.string(),
  unit: z.string().nullable(),
  availability: z.enum(["fresh", "stale", "unknown", "unsupported"]),
  observed_at: z.string().nullable(),
  expires_at: z.string().nullable(),
});
const target = z.object({
  target_id: z.string(),
  enabled: z.boolean(),
  connection: metric,
  websocket: metric,
  metrics: z.record(z.string(), metric),
  devices: z.array(z.record(z.string(), metric)).nullable(),
  node_progress: metric.extend({
    value: z.object({ value: z.number(), max: z.number() }).nullable(),
  }),
  stage: metric.extend({
    value: z
      .object({
        phase: z.enum(["executing", "execution_finished", "execution_error"]),
        node: z.string().nullable(),
      })
      .nullable(),
  }),
  capabilities: z.record(z.string(), z.object({ status: z.string(), reason: z.string() })),
});
const envelope = z.object({
  instance_id: z.string().uuid(),
  data: z.object({ targets: z.array(target) }),
});
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
  const [snapshot, setSnapshot] = useState<z.infer<typeof envelope> | null>(null);
  const [error, setError] = useState(false);
  const [probing, setProbing] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    async function update() {
      try {
        const response = await fetch("/api/status", {
          cache: "no-store",
          signal: AbortSignal.any([controller.signal, AbortSignal.timeout(4000)]),
        });
        if (!response.ok) throw new Error("status_unavailable");
        const data = envelope.parse(await response.json());
        if (!controller.signal.aborted) {
          setSnapshot(data);
          setError(false);
        }
      } catch {
        if (!controller.signal.aborted) setError(true);
      }
    }
    void update();
    const timer = window.setInterval(() => void update(), 5000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, []);

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
      const data = target.parse((await response.json()).data);
      setSnapshot((previous) =>
        previous
          ? {
              ...previous,
              data: {
                targets: previous.data.targets.map((item) => (item.target_id === id ? data : item)),
              },
            }
          : previous,
      );
      setError(false);
    } catch {
      setError(true);
    } finally {
      setProbing(null);
    }
  }
  return (
    <section className="workspace settings" aria-labelledby="monitor-title">
      <div className="section-heading">
        <h2 id="monitor-title">执行端监测</h2>
        <span>每 5 秒刷新 · 15 秒过期</span>
      </div>
      <p className="settings-note">
        只显示实际采集值。外部队列不代表本项目任务，节点步数不代表整体生成进度。
      </p>
      {error && (
        <p className="error" role="alert">
          状态读取或探测失败，保留的快照可能已过期，请等待重连。
        </p>
      )}
      {snapshot?.data.targets.length === 0 && (
        <p className="settings-note">尚未配置执行端，请先在项目设置中添加。</p>
      )}
      {snapshot?.data.targets.map((item) => (
        <article className="target-editor" key={item.target_id}>
          <div className="section-heading">
            <h3>
              {item.target_id} · {item.enabled ? "已启用" : "已停用"}
            </h3>
            <button
              type="button"
              disabled={!item.enabled || probing !== null}
              onClick={() => void probe(item.target_id)}
            >
              {probing === item.target_id ? "探测中…" : "重新探测能力"}
            </button>
          </div>
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
                  {value.availability === "stale" ||
                  (value.expires_at && Date.parse(value.expires_at) <= Date.now())
                    ? "（已过期）"
                    : ""}
                </span>
                <details className="settings-note">
                  <summary>采集来源与时间</summary>
                  <div>{value.source}</div>
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
        </article>
      ))}
    </section>
  );
}
