import { useEffect, useRef, useState } from "react";
import { z } from "zod";
import { Decisions, Delivery } from "./Decisions";
import { Feedback } from "./Feedback";
import { Monitor } from "./Monitor";
import { ProductionActivity } from "./ProductionActivity";
import { useWorkspace, workspaceSchema } from "./Workspace";

const versionSchema = workspaceSchema.shape.data.shape.versions.element;
const detailSchema = z.object({
  data: z.object({
    version: versionSchema,
    comparison: z.object({
      status: z.enum(["known", "unknown"]),
      source_asset_id: z.string().nullable(),
      offset_seconds: z.number().optional(),
      duration_seconds: z.number().optional(),
      reason: z.string(),
    }),
  }),
});
export type Version = z.infer<typeof versionSchema>;

function clipSegment(version: Version) {
  return (
    version.source.segment_id ??
    version.source.inputs.find((item) => item.slot === "video")?.asset_id ??
    version.run_id
  );
}

function clipKey(version: Version) {
  return `${version.task_id}:${clipSegment(version)}:${version.slot}`;
}

function ReviewPlayer({
  version,
  candidate,
}: {
  version: Version;
  candidate: React.RefObject<HTMLVideoElement | null>;
}) {
  const original = useRef<HTMLVideoElement>(null);
  const [detail, setDetail] = useState<z.infer<typeof detailSchema>["data"] | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    fetch(`/api/versions/${version.id}`, { signal: controller.signal, cache: "no-store" })
      .then(async (response) => {
        if (!response.ok) throw new Error();
        return detailSchema.parse(await response.json());
      })
      .then((result) => setDetail(result.data))
      .catch(() => {
        if (!controller.signal.aborted) setError("来源暂不可读，保留当前预览。");
      });
    return () => controller.abort();
  }, [version.id]);
  const mapping = detail?.comparison;
  const synchronized = mapping?.status === "known";
  const url = `/api/versions/${version.id}/content`;

  function seek(from: "candidate" | "original") {
    if (!synchronized || !candidate.current || !original.current) return;
    const offset = mapping?.offset_seconds ?? 0;
    if (from === "candidate") {
      const time = candidate.current.currentTime + offset;
      if (Math.abs(original.current.currentTime - time) > 0.08) original.current.currentTime = time;
    } else {
      const time = Math.min(
        mapping?.duration_seconds ?? Infinity,
        Math.max(0, original.current.currentTime - offset),
      );
      if (Math.abs(candidate.current.currentTime - time) > 0.08)
        candidate.current.currentTime = time;
    }
  }
  function play(from: "candidate" | "original") {
    if (!synchronized) return;
    seek(from);
    const peer = from === "candidate" ? original.current : candidate.current;
    if (peer?.paused)
      void peer.play().catch(() => setError("浏览器尚未允许同步播放，请手动播放两侧。"));
  }
  function pause(from: "candidate" | "original") {
    if (synchronized) (from === "candidate" ? original.current : candidate.current)?.pause();
  }
  return (
    <article className="review-detail">
      <div className="section-heading">
        <h3>候选 {version.id.slice(0, 8)}</h3>
        <span>检查：{version.checks.status === "passed" ? "通过" : version.checks.status}</span>
      </div>
      <p className="settings-note">
        运行 {version.run_id.slice(0, 8)} · {version.source.target_snapshot.target.name} ·{" "}
        {version.media.width} × {version.media.height} · {version.media.frame_count ?? "未知"} 帧
      </p>
      <div className="review-panes">
        <figure>
          <figcaption>候选结果</figcaption>
          {version.kind === "video" ? (
            <video
              ref={candidate}
              controls
              preload="metadata"
              src={url}
              aria-label="候选结果"
              onTimeUpdate={() => seek("candidate")}
              onSeeked={() => seek("candidate")}
              onPlay={() => play("candidate")}
              onPause={() => pause("candidate")}
              onRateChange={() => {
                if (synchronized && original.current && candidate.current)
                  original.current.playbackRate = candidate.current.playbackRate;
              }}
            >
              <track kind="captions" />
            </video>
          ) : (
            <img src={url} alt="候选结果" />
          )}
        </figure>
        <figure>
          <figcaption>{synchronized ? "原片 · 同步定位" : "输入视频 · 并排参考"}</figcaption>
          {mapping?.source_asset_id ? (
            <video
              ref={original}
              controls
              muted
              preload="metadata"
              aria-label="原片参考"
              src={`/api/assets/${mapping.source_asset_id}/content`}
              onLoadedMetadata={() => seek("candidate")}
              onSeeked={() => seek("original")}
              onPlay={() => play("original")}
              onPause={() => pause("original")}
            >
              <track kind="captions" />
            </video>
          ) : (
            <p>没有可用的视频参考。</p>
          )}
        </figure>
      </div>
      <p className="settings-note">
        {mapping?.reason ?? "正在读取时间映射…"}
        {synchronized && ` 原片时间 = 候选时间 + ${mapping?.offset_seconds} 秒。`}
      </p>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div className="settings-actions">
        <a href={`${url}?variant=original`}>下载原始结果</a>
        {mapping?.source_asset_id && (
          <a href={`/api/assets/${mapping.source_asset_id}/content`}>查看输入/原片</a>
        )}
      </div>
      <details className="media-details">
        <summary>来源与技术证据</summary>
        <p>版本 {version.id}</p>
        <p>内容 SHA-256：{version.content_hash}</p>
        <p>最终图 SHA-256：{version.source.graph_hash}</p>
        <p>外部回执：{version.source.prompt_id}</p>
        <p>收片：{new Date(version.created_at).toLocaleString("zh-CN")}</p>
      </details>
    </article>
  );
}

export function Versions() {
  const candidate = useRef<HTMLVideoElement>(null);
  const { snapshot, stale } = useWorkspace();
  const versions = snapshot?.data.versions ?? [];
  const [clip, setClip] = useState("");
  const [choices, setChoices] = useState<Record<string, string>>({});
  const groups = new Map<string, Version[]>();
  for (const version of versions.filter((item) => item.ready)) {
    const key = clipKey(version);
    groups.set(key, [...(groups.get(key) ?? []), version]);
  }
  useEffect(() => {
    if (!snapshot) return;
    const first = snapshot.data.versions.find((item) => item.ready);
    if (first) {
      const key = clipKey(first);
      setClip((previous) => previous || key);
    }
    setChoices((previous) => {
      const next = { ...previous };
      for (const version of snapshot.data.versions.filter((item) => item.ready)) {
        const key = clipKey(version);
        if (!next[key]) next[key] = version.id;
      }
      return next;
    });
  }, [snapshot]);
  const activeClip = groups.has(clip) ? clip : (groups.keys().next().value ?? "");
  const candidates = groups.get(activeClip) ?? [];
  const selected = candidates.find((item) => item.id === choices[activeClip]) ?? candidates[0];
  const phases: Record<string, string> = {
    waiting_output: "等待输出",
    transferring: "传输中",
    checking: "校验中",
    proxy: "制作播放代理",
    registering: "登记版本",
    blocked: "已阻塞",
    retry_wait: "等待重试",
    failed: "检查失败",
  };
  return (
    <section className="workspace settings" aria-labelledby="versions-title">
      <div className="section-heading">
        <h2 id="versions-title">逐段审片</h2>
        <span>REVIEW DESK</span>
      </div>
      <p className="settings-note">
        每段完成检查后立即可审，无需等待其他生成。切换片段或候选由你决定。
      </p>
      {stale && (
        <p role="alert" className="error">
          审片状态暂不可用，保留已有预览；正在重连。
        </p>
      )}
      {snapshot?.data.collections.map((item) => (
        <p key={item.run_id} role="status">
          {item.run_id.slice(0, 8)} · {phases[item.phase] ?? item.phase}
          {item.bytes_done !== undefined &&
            ` · ${item.bytes_done} / ${item.bytes_total ?? "未知"} 字节`}
          {item.error && ` · ${item.error.message}`}
        </p>
      ))}
      {!selected && <p>等待首个可审版本。</p>}
      <div className="review-desk">
        <nav className="review-clips" aria-label="审片片段">
          {Array.from(groups, ([key, items]) => (
            <button
              type="button"
              key={key}
              title={clipSegment(items[0])}
              aria-pressed={key === activeClip}
              onClick={() => setClip(key)}
            >
              片段 {clipSegment(items[0]).slice(0, 8)}
              <small>
                {items.length} 个候选 · {items[0].slot}
              </small>
            </button>
          ))}
        </nav>
        {selected && (
          <div className="review-stage">
            <label>
              审阅候选
              <select
                aria-label="审阅候选"
                value={selected.id}
                onChange={(event) =>
                  setChoices((previous) => ({ ...previous, [activeClip]: event.target.value }))
                }
              >
                {candidates.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.id.slice(0, 8)} · {item.source.target_snapshot.target.id} · 检查通过
                  </option>
                ))}
              </select>
            </label>
            <ReviewPlayer key={selected.id} version={selected} candidate={candidate} />
            <Decisions version={selected} />
            <Feedback
              version={selected}
              video={candidate}
              onLocate={(id, time) => {
                const version = versions.find((item) => item.id === id);
                if (!version) return;
                const key = clipKey(version);
                setClip(key);
                setChoices((previous) => ({ ...previous, [key]: id }));
                window.setTimeout(() => {
                  if (candidate.current) candidate.current.currentTime = time;
                }, 100);
              }}
            />
          </div>
        )}
        <Monitor />
      </div>
      <ProductionActivity />
      <Delivery />
    </section>
  );
}
