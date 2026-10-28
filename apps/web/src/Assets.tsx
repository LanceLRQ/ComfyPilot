import { useEffect, useRef, useState } from "react";
import { z } from "zod";

const assetSchema = z
  .object({
    id: z.string(),
    filename: z.string(),
    kind: z.enum(["video", "image"]),
    role: z.string(),
    content_hash: z.string(),
    size: z.number(),
    media: z
      .object({
        width: z.number(),
        height: z.number(),
        duration_seconds: z.number().nullable(),
      })
      .passthrough(),
  })
  .passthrough();
const snapshotSchema = z.object({
  assets: z.array(assetSchema),
  jobs: z.array(
    z.object({
      id: z.string(),
      kind: z.string(),
      phase: z.string(),
      bytes_done: z.number(),
      error: z.object({ message: z.string() }).nullable(),
    }),
  ),
  limits: z.object({ video_bytes: z.number(), image_bytes: z.number() }),
});
const envelope = z.object({
  instance_id: z.string(),
  data: z.unknown().optional(),
  error: z.object({ message: z.string() }).optional(),
});
const phases: Record<string, string> = {
  queued: "排队中",
  preparing: "准备中",
  copying: "复制中",
  extracting: "提取中",
  decoding: "校验解码中",
  ready: "已就绪",
  blocked: "已阻塞",
  failed: "失败",
};

export function Assets() {
  const [snapshot, setSnapshot] = useState<z.infer<typeof snapshotSchema> | null>(null);
  const [instance, setInstance] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [kind, setKind] = useState<"video" | "image">("video");
  const [source, setSource] = useState("");
  const [reference, setReference] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [fps, setFps] = useState(24);
  const [width, setWidth] = useState(768);
  const [height, setHeight] = useState(448);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);
  const [message, setMessage] = useState("");
  const [readError, setReadError] = useState(false);
  const upload = useRef<XMLHttpRequest | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    async function refresh() {
      try {
        const response = await fetch("/api/assets", {
          cache: "no-store",
          signal: AbortSignal.any([controller.signal, AbortSignal.timeout(4000)]),
        });
        const result = envelope.parse(await response.json());
        if (!response.ok || result.error) throw new Error("assets_unavailable");
        const data = snapshotSchema.parse(result.data);
        if (controller.signal.aborted) return;
        setSnapshot(data);
        setInstance(result.instance_id);
        setReadError(false);
      } catch {
        if (!controller.signal.aborted) setReadError(true);
      }
    }
    void refresh();
    const timer = window.setInterval(() => void refresh(), 1500);
    return () => {
      controller.abort();
      window.clearInterval(timer);
      upload.current?.abort();
    };
  }, []);

  function sendFile() {
    if (!file) return;
    if (snapshot && file.size > snapshot.limits[`${kind}_bytes`]) {
      setMessage("文件超过项目导入上限，请先裁剪或在项目设置调整限额。");
      return;
    }
    setBusy(true);
    setMessage("");
    setProgress(0);
    const xhr = new XMLHttpRequest();
    upload.current = xhr;
    xhr.open("POST", `/api/assets/upload?${new URLSearchParams({ kind, filename: file.name })}`);
    xhr.setRequestHeader("X-ComfyPilot-Instance", instance);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) setProgress(Math.round((100 * event.loaded) / event.total));
    };
    xhr.onload = () => {
      try {
        const result = envelope.parse(JSON.parse(xhr.responseText));
        if (xhr.status >= 400 || result.error) throw new Error(result.error?.message ?? "上传失败");
        setMessage("文件已上传，等待后台复制与解码完成。请查看任务状态。");
      } catch (error) {
        setMessage(error instanceof Error ? error.message : "上传响应无效，请检查任务列表。");
      }
    };
    xhr.onerror = () => setMessage("上传连接中断，请先检查任务列表再决定是否重新上传。");
    xhr.onabort = () => setMessage("上传已取消；若服务器已接收完毕，请检查任务列表。");
    xhr.onloadend = () => {
      setBusy(false);
      setProgress(null);
      upload.current = null;
    };
    xhr.send(file);
  }

  async function extract() {
    const from = Number(start),
      to = Number(end);
    if (
      to <= from ||
      [from, to].some((value) => Math.abs(value * fps - Math.round(value * fps)) > 1e-6)
    ) {
      setMessage("起止时间必须递增，并对齐输出帧率；24 FPS 下每帧为 1/24 秒。");
      return;
    }
    setBusy(true);
    setMessage("");
    try {
      const response = await fetch("/api/assets/clips", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-ComfyPilot-Instance": instance },
        signal: AbortSignal.timeout(5000),
        body: JSON.stringify({
          source_asset_id: source,
          reference_asset_id: reference || null,
          start_seconds: from,
          end_seconds: to,
          fps,
          width,
          height,
        }),
      });
      const result = envelope.parse(await response.json());
      if (!response.ok || result.error) throw new Error(result.error?.message ?? "提取提交失败");
      setMessage("提取任务已排队，后台将保存工作片、原片映射和输入顺序。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "请求失败，请先检查任务列表。");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="workspace settings" aria-labelledby="assets-title">
      <div className="section-heading">
        <h2 id="assets-title">素材与显式片段</h2>
        <span>LOCAL MEDIA</span>
      </div>
      <p className="settings-note">
        原片与参考图保存在当前项目。提取前明确指定起止时间；工作片无音轨，原片保持原样。
      </p>
      {readError && (
        <p className="error" role="alert">
          素材状态读取失败，正在重连；保留上次数据。
        </p>
      )}
      {message && (
        <p className="settings-message" role="status">
          {message}
        </p>
      )}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          sendFile();
        }}
      >
        <fieldset disabled={busy || !instance}>
          <div className="settings-grid">
            <label>
              素材类型
              <select
                value={kind}
                onChange={(event) => {
                  setKind(event.target.value === "image" ? "image" : "video");
                  setFile(null);
                }}
              >
                <option value="video">视频原片</option>
                <option value="image">人物参考图</option>
              </select>
            </label>
            <label>
              选择本机文件
              <input
                key={kind}
                type="file"
                required
                accept={kind === "video" ? ".mp4,.mov,.mkv,.webm" : ".png,.jpg,.jpeg,.webp"}
                onChange={(event) => setFile(event.target.files?.[0] ?? null)}
              />
            </label>
          </div>
          <p className="settings-note">
            当前上限：视频 {snapshot ? snapshot.limits.video_bytes / 1048576 : "—"} MiB，图片{" "}
            {snapshot ? snapshot.limits.image_bytes / 1048576 : "—"} MiB。
          </p>
          <button type="submit" disabled={!file}>
            上传并导入
          </button>
        </fieldset>
      </form>
      {progress !== null && (
        <div className="resource-actions">
          <progress max="100" value={progress} aria-label="文件上传进度" />
          <span>上传 {progress}% · 后续解码以任务状态为准</span>
          <button type="button" onClick={() => upload.current?.abort()}>
            取消上传
          </button>
        </div>
      )}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void extract();
        }}
      >
        <fieldset disabled={busy || !instance}>
          <legend>提取工作片</legend>
          <div className="settings-grid">
            <label>
              原片
              <select required value={source} onChange={(event) => setSource(event.target.value)}>
                <option value="">请选择已导入原片</option>
                {snapshot?.assets
                  .filter((asset) => asset.role === "original")
                  .map((asset) => (
                    <option key={asset.id} value={asset.id}>
                      {asset.filename} · {asset.media.duration_seconds} 秒 · {asset.id.slice(0, 8)}
                    </option>
                  ))}
              </select>
            </label>
            <label>
              人物参考图
              <select value={reference} onChange={(event) => setReference(event.target.value)}>
                <option value="">暂不绑定</option>
                {snapshot?.assets
                  .filter((asset) => asset.kind === "image")
                  .map((asset) => (
                    <option key={asset.id} value={asset.id}>
                      {asset.filename} · {asset.id.slice(0, 8)}
                    </option>
                  ))}
              </select>
            </label>
            <label>
              起点（秒，含）
              <input
                required
                type="number"
                min="0"
                step="any"
                value={start}
                onChange={(event) => setStart(event.target.value)}
              />
            </label>
            <label>
              终点（秒，不含）
              <input
                required
                type="number"
                min="0"
                step="any"
                value={end}
                onChange={(event) => setEnd(event.target.value)}
              />
            </label>
          </div>
          <details className="media-details">
            <summary>工作片规格</summary>
            <div className="settings-grid">
              <label>
                输出 FPS
                <input
                  required
                  type="number"
                  min="1"
                  max="120"
                  value={fps}
                  onChange={(event) => setFps(Number(event.target.value))}
                />
              </label>
              <label>
                宽度（偶数）
                <input
                  required
                  type="number"
                  min="32"
                  max="4096"
                  step="2"
                  value={width}
                  onChange={(event) => setWidth(Number(event.target.value))}
                />
              </label>
              <label>
                高度（偶数）
                <input
                  required
                  type="number"
                  min="32"
                  max="4096"
                  step="2"
                  value={height}
                  onChange={(event) => setHeight(Number(event.target.value))}
                />
              </label>
            </div>
            <p className="settings-note">
              等比缩放并补边。H3 配方另有固定帧数要求，提交生成前仍需检查。
            </p>
          </details>
          <button type="submit" disabled={!source}>
            提取显式片段
          </button>
        </fieldset>
      </form>
      <h3>后台媒体任务</h3>
      <p className="settings-note">
        最近 50 项，每 1.5 秒刷新。失败或中断任务由 Agent 或用户重新提交。
      </p>
      <ul className="media-jobs">
        {snapshot?.jobs.map((job) => (
          <li key={job.id}>
            <code>{job.id.slice(0, 8)}</code> · {job.kind} · {phases[job.phase] ?? job.phase} ·{" "}
            {(job.bytes_done / 1048576).toFixed(2)} MiB
            {job.error && <p className="error">{job.error.message}</p>}
          </li>
        ))}
      </ul>
      <h3>受管素材</h3>
      <p className="settings-note">最近 100 项；相同内容可共享存储，每次导入保留独立身份。</p>
      <div className="media-grid">
        {snapshot?.assets.map((asset) => (
          <article className="target-editor" key={asset.id}>
            <strong>{asset.filename}</strong>
            {asset.kind === "video" ? (
              <video controls preload="metadata" src={`/api/assets/${asset.id}/content`}>
                <track kind="captions" />
              </video>
            ) : (
              <img
                loading="lazy"
                src={`/api/assets/${asset.id}/content`}
                alt={`参考图：${asset.filename}`}
              />
            )}
            <p>
              {asset.role} · {asset.media.width} × {asset.media.height} ·{" "}
              {(asset.size / 1048576).toFixed(2)} MiB
            </p>
            <details>
              <summary>哈希、来源与时间映射</summary>
              <pre className="resource-code">{JSON.stringify(asset, null, 2)}</pre>
            </details>
          </article>
        ))}
      </div>
    </section>
  );
}
