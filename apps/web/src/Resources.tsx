import { useEffect, useState } from "react";
import { z } from "zod";

const resource = z.object({
  id: z.string(),
  source: z.string(),
  hash: z.string(),
  manifest: z.record(z.string(), z.unknown()).nullable(),
  api_graph: z.record(z.string(), z.unknown()).optional(),
  files: z.record(z.string(), z.string()),
  errors: z.array(z.string()),
  overridden_sources: z.array(z.string()),
  states: z.object({
    discovered: z.boolean(),
    declaration_complete: z.boolean(),
    real_test: z.string(),
  }),
});
const preflight = z.object({
  recipe_hash: z.string(),
  target_id: z.string(),
  status: z.string(),
  stale: z.boolean().optional(),
  missing_nodes: z.array(z.string()),
  missing_models: z.array(z.string()),
  errors: z.array(z.string()),
  expires_at: z.string().nullable(),
});
const catalog = z.object({
  revision: z.number(),
  hash: z.string().nullable(),
  modules: z.array(resource),
  recipes: z.array(resource),
  custom_nodes: z.array(resource),
  files: z.array(z.object({ source: z.string(), hash: z.string(), size: z.number() })),
  preflights: z.array(preflight),
});
const envelope = z.object({
  instance_id: z.string(),
  data: z.unknown().optional(),
  error: z.object({ message: z.string() }).optional(),
});
const labels: Record<string, string> = {
  passed: "标准预检通过",
  missing_dependencies: "缺少依赖",
  invalid: "定义不匹配",
  unknown: "未知",
};

async function request(path: string, body?: unknown, instance = "", signal?: AbortSignal) {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json", "X-ComfyPilot-Instance": instance },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
    signal: signal
      ? AbortSignal.any([signal, AbortSignal.timeout(5000)])
      : AbortSignal.timeout(5000),
  });
  const result = envelope.parse(await response.json());
  if (!response.ok || result.error) throw new Error(result.error?.message ?? "资源请求失败");
  return result;
}

export function Resources() {
  const [snapshot, setSnapshot] = useState<z.infer<typeof catalog> | null>(null);
  const [instance, setInstance] = useState("");
  const [targets, setTargets] = useState<string[]>([]);
  const [target, setTarget] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [original, setOriginal] = useState<{ source: string; text: string } | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      request("/api/resources", undefined, "", controller.signal),
      request("/api/targets", undefined, "", controller.signal),
    ])
      .then(([resources, configured]) => {
        if (controller.signal.aborted) return;
        setSnapshot(catalog.parse(resources.data));
        setInstance(resources.instance_id);
        const data = z
          .object({ targets: z.array(z.object({ id: z.string(), enabled: z.boolean() })) })
          .parse(configured.data);
        setTargets(data.targets.filter((item) => item.enabled).map((item) => item.id));
      })
      .catch(() => {
        if (!controller.signal.aborted) setMessage("资源读取失败，请重新读取。");
      });
    return () => controller.abort();
  }, []);

  async function perform(action: "read" | "refresh" | "file" | "preflight", value = "") {
    setBusy(true);
    setMessage("");
    try {
      if (action === "file") {
        const file = snapshot?.files.find((item) => item.source === value);
        if (!file) throw new Error("文件尚未发现");
        const result = await request("/api/resources/file", { resource_hash: file.hash }, instance);
        const data = z.object({ text: z.string().nullable() }).parse(result.data);
        setOriginal({ source: file.source, text: data.text ?? "二进制文件，仅保存内容与哈希。" });
      } else {
        if (action === "preflight") {
          if (!target) throw new Error("请明确选择一个已启用的执行端。");
          await request(
            "/api/resources/preflight",
            { recipe_id: value, target_id: target },
            instance,
          );
        }
        const result = await request(
          action === "refresh" ? "/api/resources/refresh" : "/api/resources",
          action === "refresh" ? { expected_revision: snapshot?.revision ?? 0 } : undefined,
          instance,
        );
        setSnapshot(catalog.parse(result.data));
        setInstance(result.instance_id);
        if (action === "read") {
          const configured = await request("/api/targets");
          const data = z
            .object({ targets: z.array(z.object({ id: z.string(), enabled: z.boolean() })) })
            .parse(configured.data);
          setTargets(data.targets.filter((item) => item.enabled).map((item) => item.id));
        }
        setMessage(
          action === "refresh" ? "资源刷新成功；旧方案和运行保持冻结内容。" : "已读取有效快照。",
        );
      }
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "资源操作失败，保留上次快照。");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="workspace settings" aria-labelledby="resources-title">
      <div className="section-heading">
        <h2 id="resources-title">模块、配方与资源</h2>
        <span>资源修订 {snapshot?.revision ?? "—"}</span>
      </div>
      <p className="settings-note">
        Agent
        修改项目资源后，显式刷新才会生效。画布原件、自定义节点代码只供查看；平台不会导入或安装代码。
      </p>
      <div className="resource-actions">
        <button type="button" disabled={busy} onClick={() => void perform("read")}>
          重新读取资源
        </button>
        <button type="button" disabled={busy || !snapshot} onClick={() => void perform("refresh")}>
          刷新磁盘资源
        </button>
        <label>
          预检执行端
          <select value={target} onChange={(event) => setTarget(event.target.value)}>
            <option value="">请选择执行端</option>
            {targets.map((id) => (
              <option key={id} value={id}>
                {id}
              </option>
            ))}
          </select>
        </label>
      </div>
      {message && (
        <p role="status" className="settings-message">
          {message}
        </p>
      )}
      {snapshot?.revision === 0 && (
        <p className="settings-note">尚未刷新资源。内置模块与项目资源会在首次显式刷新后出现。</p>
      )}
      {(
        [
          ["模块", snapshot?.modules],
          ["配方", snapshot?.recipes],
          ["自定义节点", snapshot?.custom_nodes],
        ] as const
      ).map(([name, records]) => (
        <div key={name}>
          <h3>
            {name} · {records?.length ?? 0}
          </h3>
          {records?.map((item) => (
            <article className="target-editor" key={item.source}>
              <h4>
                {String(item.manifest?.name ?? item.id)} ·{" "}
                {String(item.manifest?.version ?? "声明损坏")}
              </h4>
              <p>{String(item.manifest?.description ?? "请查看声明原件并修正格式。")}</p>
              <p className="settings-note">
                来源：{item.source}
                <br />
                内容哈希：<code>{item.hash}</code>
              </p>
              <p>
                已发现 · {item.states.declaration_complete ? "声明完整" : "声明不完整"} ·
                真实生成：未实测
              </p>
              {item.errors.map((error) => (
                <p className="error" key={error}>
                  {error}
                </p>
              ))}
              {item.overridden_sources.length > 0 && (
                <p>已显式覆盖：{item.overridden_sources.join("、")}</p>
              )}
              {name === "配方" && (
                <>
                  <button
                    type="button"
                    disabled={busy || !item.states.declaration_complete || !target}
                    onClick={() => void perform("preflight", item.id)}
                  >
                    预检此配方
                  </button>
                  {snapshot?.preflights
                    .filter((report) => report.recipe_hash === item.hash)
                    .map((report) => (
                      <p key={report.target_id}>
                        {report.target_id}：{labels[report.status] ?? report.status}
                        {report.stale ||
                        !report.expires_at ||
                        Date.parse(report.expires_at) <= Date.now()
                          ? "（已过期，请重新探测后预检）"
                          : ""}{" "}
                        · 缺少节点：{report.missing_nodes.join("、") || "无"} · 缺少模型：
                        {report.missing_models.join("、") || "无"} {report.errors.join("；")}
                      </p>
                    ))}
                  {!snapshot?.preflights.some((report) => report.recipe_hash === item.hash) && (
                    <p>目标依赖：未知 · 尚未预检</p>
                  )}
                  <details>
                    <summary>可执行 API 图模板</summary>
                    <pre className="resource-code">
                      {item.api_graph
                        ? JSON.stringify(item.api_graph, null, 2)
                        : "尚无通过声明校验的 API 图。"}
                    </pre>
                  </details>
                </>
              )}
              <details>
                <summary>声明、绑定与依赖说明</summary>
                <pre className="resource-code">{JSON.stringify(item.manifest, null, 2)}</pre>
              </details>
            </article>
          ))}
        </div>
      ))}
      <label>
        查看快照原件
        <select
          disabled={busy || !snapshot?.files.length}
          value={original?.source ?? ""}
          onChange={(event) => void perform("file", event.target.value)}
        >
          <option value="">请选择文件</option>
          {snapshot?.files.map((file) => (
            <option key={file.source} value={file.source}>
              {file.source}
            </option>
          ))}
        </select>
      </label>
      {original && (
        <div>
          <h3>{original.source}</h3>
          <pre className="resource-code">{original.text}</pre>
        </div>
      )}
      <p className="settings-note">
        标准预检只检查节点 schema。媒体路径待上传阶段物化，自定义验证、版本和真实生成需另行验收。
      </p>
    </section>
  );
}
