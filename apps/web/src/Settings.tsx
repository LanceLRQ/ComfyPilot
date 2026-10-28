import { useEffect, useState } from "react";
import { z } from "zod";

const targetSchema = z.object({
  id: z.string(),
  name: z.string(),
  location: z.enum(["local", "remote"]),
  base_url: z.string(),
  enabled: z.boolean(),
  credential_ref: z.string().nullable(),
  assets: z
    .object({ mode: z.literal("local"), input_dir: z.string(), output_dir: z.string() })
    .nullable(),
});
const configSchema = z.object({
  schema_version: z.literal(1),
  name: z.string(),
  server: z.object({ host: z.literal("127.0.0.1"), port: z.number().int() }),
  targets: z.array(targetSchema),
  default_target_id: z.string().nullable(),
  resource_overrides: z.record(z.string(), z.string()),
  import_limits: z.object({
    video_bytes: z.number().int(),
    image_bytes: z.number().int(),
    workflow_bytes: z.number().int(),
  }),
});
const snapshotSchema = z.object({
  revision: z.number().int(),
  config: configSchema,
  file_changed: z.boolean(),
  warnings: z.array(
    z.object({ code: z.string(), message: z.string(), target_ids: z.array(z.string()) }),
  ),
  credentials: z.record(z.string(), z.boolean()),
});
const replySchema = z.object({
  instance_id: z.string().uuid(),
  data: snapshotSchema.optional(),
  error: z.object({ code: z.string(), message: z.string() }).optional(),
});
type Config = z.infer<typeof configSchema>;
type Snapshot = z.infer<typeof snapshotSchema>;

async function request(path: string, body?: unknown, instance?: string, signal?: AbortSignal) {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: {
      "Content-Type": "application/json",
      ...(instance ? { "X-ComfyPilot-Instance": instance } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: signal
      ? AbortSignal.any([signal, AbortSignal.timeout(5000)])
      : AbortSignal.timeout(5000),
    cache: "no-store",
  });
  const result = replySchema.parse(await response.json());
  if (!response.ok || result.error || !result.data)
    throw new Error(result.error?.message ?? "配置暂时不可用，请检查服务。 ");
  return { snapshot: result.data, instance: result.instance_id };
}

export function Settings() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [draft, setDraft] = useState<Config | null>(null);
  const [instance, setInstance] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    request("/api/config", undefined, undefined, controller.signal)
      .then(({ snapshot, instance }) => {
        setSnapshot(snapshot);
        setDraft(snapshot.config);
        setInstance(instance);
      })
      .catch(() => {
        if (!controller.signal.aborted) setMessage("无法读取配置，请检查服务后重新读取。");
      });
    return () => controller.abort();
  }, []);

  async function apply(action: "read" | "save" | "refresh") {
    if (action !== "read" && (!snapshot || !draft)) return;
    setBusy(true);
    setMessage("");
    try {
      const path =
        action === "read"
          ? "/api/config"
          : `/api/config/${action === "save" ? "update" : "refresh"}`;
      const body =
        action === "read"
          ? undefined
          : {
              expected_revision: snapshot?.revision,
              ...(action === "save" ? { config: draft } : {}),
            };
      const result = await request(path, body, instance);
      setSnapshot(result.snapshot);
      setDraft(result.snapshot.config);
      setInstance(result.instance);
      setMessage(action === "read" ? "已读取最新有效配置。" : "配置已保存为新的有效修订。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "操作失败，当前草稿已保留。");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="workspace settings" aria-labelledby="settings-title">
      <div className="section-heading">
        <h2 id="settings-title">项目设置与执行端</h2>
        <span>{snapshot ? `配置修订 ${snapshot.revision}` : "正在读取配置"}</span>
      </div>
      <p className="settings-note">
        Agent 和此页面共用配置。手改 comfypilot.yaml 后，点击“接纳文件修改”才生效。
      </p>
      {snapshot?.file_changed && (
        <p className="error">磁盘配置有未接纳的修改，当前继续使用有效修订。</p>
      )}
      {message && (
        <p role="status" className="settings-message">
          {message}
        </p>
      )}
      {draft && (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void apply("save");
          }}
        >
          <fieldset disabled={busy}>
            <div className="settings-grid">
              <label>
                项目名称
                <input
                  required
                  value={draft.name}
                  onChange={(event) => setDraft({ ...draft, name: event.target.value })}
                />
              </label>
              <label>
                服务端口（重启时生效）
                <input
                  required
                  type="number"
                  min="1"
                  max="65535"
                  value={draft.server.port}
                  onChange={(event) =>
                    setDraft({
                      ...draft,
                      server: { ...draft.server, port: Number(event.target.value) },
                    })
                  }
                />
              </label>
              <label>
                默认执行端
                <select
                  value={draft.default_target_id ?? ""}
                  onChange={(event) =>
                    setDraft({ ...draft, default_target_id: event.target.value || null })
                  }
                >
                  <option value="">不设默认，执行前询问</option>
                  {draft.targets.map((target) => (
                    <option key={target.id} value={target.id}>
                      {target.name || target.id}
                      {target.enabled ? "" : "（已停用）"}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <details className="media-details">
              <summary>素材与工作流导入上限（字节）</summary>
              <div className="settings-grid">
                {(["video_bytes", "image_bytes", "workflow_bytes"] as const).map((key) => (
                  <label key={key}>
                    {
                      { video_bytes: "视频", image_bytes: "图片", workflow_bytes: "工作流单文件" }[
                        key
                      ]
                    }
                    <input
                      required
                      type="number"
                      min="1"
                      step="1"
                      value={draft.import_limits[key]}
                      onChange={(event) =>
                        setDraft({
                          ...draft,
                          import_limits: {
                            ...draft.import_limits,
                            [key]: Number(event.target.value),
                          },
                        })
                      }
                    />
                  </label>
                ))}
              </div>
            </details>
            <div className="target-list">
              {draft.targets.map((target) => {
                function change(patch: Partial<z.infer<typeof targetSchema>>) {
                  setDraft(
                    (current) =>
                      current && {
                        ...current,
                        targets: current.targets.map((item) =>
                          item.id === target.id ? { ...item, ...patch } : item,
                        ),
                      },
                  );
                }
                return (
                  <article
                    className="target-editor"
                    key={target.id}
                    aria-label={`执行端 ${target.name || target.id}`}
                  >
                    <div className="section-heading">
                      <code>{target.id}</code>
                      <button
                        type="button"
                        className="text-button"
                        onClick={() =>
                          setDraft({
                            ...draft,
                            targets: draft.targets.filter((item) => item.id !== target.id),
                            default_target_id:
                              draft.default_target_id === target.id
                                ? null
                                : draft.default_target_id,
                          })
                        }
                      >
                        移出配置
                      </button>
                    </div>
                    <div className="settings-grid">
                      <label>
                        执行端名称
                        <input
                          required
                          value={target.name}
                          onChange={(event) => change({ name: event.target.value })}
                        />
                      </label>
                      <label>
                        部署位置
                        <select
                          value={target.location}
                          onChange={(event) =>
                            change({
                              location: event.target.value === "local" ? "local" : "remote",
                            })
                          }
                        >
                          <option value="local">本机</option>
                          <option value="remote">远程</option>
                        </select>
                      </label>
                      <label className="target-address">
                        ComfyUI 地址
                        <input
                          required
                          type="url"
                          placeholder="http://127.0.0.1:8188"
                          value={target.base_url}
                          onChange={(event) => change({ base_url: event.target.value })}
                        />
                      </label>
                      <label>
                        凭据引用（可选）
                        <input
                          value={target.credential_ref ?? ""}
                          placeholder="只填引用名，不填密钥"
                          onChange={(event) =>
                            change({ credential_ref: event.target.value || null })
                          }
                        />
                      </label>
                      <label className="checkbox">
                        <input
                          type="checkbox"
                          checked={target.assets !== null}
                          onChange={(event) =>
                            change({
                              assets: event.target.checked
                                ? {
                                    mode: "local",
                                    input_dir: "",
                                    output_dir: "",
                                  }
                                : null,
                            })
                          }
                        />
                        配置本机可访问的素材目录
                      </label>
                      {target.assets &&
                        (["input_dir", "output_dir"] as const).map((key) => (
                          <label key={key}>
                            {key === "input_dir"
                              ? "ComfyUI input 绝对路径"
                              : "ComfyUI output 绝对路径"}
                            <input
                              required
                              value={target.assets?.[key] ?? ""}
                              onChange={(event) =>
                                change({
                                  assets: {
                                    mode: "local",
                                    input_dir: target.assets?.input_dir ?? "",
                                    output_dir: target.assets?.output_dir ?? "",
                                    [key]: event.target.value,
                                  },
                                })
                              }
                            />
                          </label>
                        ))}
                      <label className="checkbox">
                        <input
                          type="checkbox"
                          checked={target.enabled}
                          onChange={(event) => change({ enabled: event.target.checked })}
                        />
                        启用此执行端
                      </label>
                    </div>
                    {target.credential_ref && (
                      <p className="settings-note">
                        凭据{snapshot?.credentials[target.credential_ref] ? "已配置" : "尚未配置"}
                        ；请让 Agent 在项目秘密文件中管理。
                      </p>
                    )}
                  </article>
                );
              })}
            </div>
            {!draft.targets.length && (
              <p className="settings-note">
                尚无执行端。请添加本机或远程 ComfyUI；平台不会猜测或自动启动目标。
              </p>
            )}
            {snapshot?.warnings.map((warning) => (
              <p className="error" key={`${warning.code}:${warning.target_ids.join(",")}`}>
                {warning.message}
              </p>
            ))}
            <div className="settings-actions">
              <button
                type="button"
                onClick={() =>
                  setDraft({
                    ...draft,
                    targets: [
                      ...draft.targets,
                      {
                        id: `target-${crypto.randomUUID().slice(0, 8)}`,
                        name: "",
                        location: "remote",
                        base_url: "",
                        enabled: true,
                        credential_ref: null,
                        assets: null,
                      },
                    ],
                  })
                }
              >
                添加执行端
              </button>
              <button type="submit" className="primary">
                保存配置
              </button>
              <button type="button" onClick={() => void apply("refresh")}>
                接纳文件修改
              </button>
            </div>
          </fieldset>
        </form>
      )}
      <button
        type="button"
        className="text-button"
        disabled={busy}
        onClick={() => void apply("read")}
      >
        重新读取有效配置（替换当前草稿）
      </button>
    </section>
  );
}
