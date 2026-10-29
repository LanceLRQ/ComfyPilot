import { useEffect, useRef, useState } from "react";
import { z } from "zod";
import { createComparisonPlayback, type PlaybackState } from "./comparison";
import { Decisions, Delivery } from "./Decisions";
import { Feedback } from "./Feedback";
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
  controllerRef,
}: {
  version: Version;
  candidate: React.RefObject<HTMLVideoElement | null>;
  controllerRef: React.RefObject<ReturnType<typeof createComparisonPlayback> | null>;
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
  const [playback, setPlayback] = useState<PlaybackState>({
    time: 0,
    duration: 0,
    frameSeconds: 0,
    playing: false,
    status: "loading",
    message: "正在加载视频…",
  });
  useEffect(() => {
    if (version.kind !== "video" || !mapping || !candidate.current) return;
    const controller = createComparisonPlayback(
      candidate.current,
      original.current,
      {
        offset: mapping.status === "known" ? mapping.offset_seconds : 0,
        durationLimit: mapping.status === "known" ? mapping.duration_seconds : undefined,
        fps: version.media.fps,
      },
      setPlayback,
    );
    controllerRef.current = controller;
    return () => {
      controller.destroy();
      controllerRef.current = null;
    };
  }, [candidate, controllerRef, mapping, version.kind, version.media.fps]);
  const url = `/api/versions/${version.id}/content`;

  return (
    <article className="review-detail">
      <div className="review-panes">
        <figure>
          <figcaption>候选结果</figcaption>
          {version.kind === "video" ? (
            <video ref={candidate} muted playsInline preload="auto" src={url} aria-label="候选结果">
              <track kind="captions" />
            </video>
          ) : (
            <img src={url} alt="候选结果" />
          )}
        </figure>
        <figure>
          <figcaption>
            {mapping?.status === "known" ? "原片 · 按映射对照" : "输入视频 · 按时间对照"}
          </figcaption>
          {mapping?.source_asset_id ? (
            <video
              ref={original}
              controls={version.kind !== "video"}
              muted
              playsInline
              preload="auto"
              aria-label="原片参考"
              src={`/api/assets/${mapping.source_asset_id}/content`}
            >
              <track kind="captions" />
            </video>
          ) : (
            <p>没有可用的视频参考。</p>
          )}
        </figure>
      </div>
      {version.kind === "video" && (
        <>
          <fieldset className="comparison-controls" aria-label="对照播放控制">
            <button
              type="button"
              disabled={!playback.duration}
              onClick={() => controllerRef.current?.toggle()}
            >
              {playback.playing ? "暂停" : playback.status === "ended" ? "重播" : "播放"}
            </button>
            <button
              type="button"
              aria-label="上一帧"
              disabled={!playback.duration || !playback.frameSeconds}
              onClick={() => controllerRef.current?.step(-1)}
            >
              −1 帧
            </button>
            <button
              type="button"
              aria-label="下一帧"
              disabled={!playback.duration || !playback.frameSeconds}
              onClick={() => controllerRef.current?.step(1)}
            >
              +1 帧
            </button>
            <input
              type="range"
              aria-label="对照播放进度"
              min="0"
              max={playback.duration}
              step="0.001"
              value={playback.time}
              disabled={!playback.duration}
              onChange={(event) => controllerRef.current?.seek(Number(event.target.value))}
            />
            <output>
              {playback.time.toFixed(3)} / {playback.duration.toFixed(3)} 秒
            </output>
          </fieldset>
          <p role="status" className={playback.status === "error" ? "error" : "settings-note"}>
            {playback.message} · 按时间对照，不保证动作逐帧一致。
            {!playback.frameSeconds && "视频帧率未知，逐帧查看暂不可用。"}
          </p>
        </>
      )}
      <Decisions version={version} />
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <details className="media-details">
        <summary>来源、下载与时间映射</summary>
        <p className="settings-note">
          候选 {version.id.slice(0, 8)} · {version.source.target_snapshot.target.name} ·{" "}
          {version.media.width} × {version.media.height} · {version.media.frame_count ?? "未知"} 帧
          · 检查：{version.checks.status === "passed" ? "通过" : version.checks.status}
        </p>

        <p className="settings-note">
          {mapping?.reason ?? "正在读取时间映射…"}
          {mapping?.status === "known" && ` 原片时间 = 候选时间 + ${mapping.offset_seconds} 秒。`}
        </p>

        <div className="settings-actions">
          <a href={`${url}?variant=original`}>下载原始结果</a>
          {mapping?.source_asset_id && (
            <a href={`/api/assets/${mapping.source_asset_id}/content`}>查看输入/原片</a>
          )}
        </div>
        <p>版本 {version.id}</p>
        <p>内容 SHA-256：{version.content_hash}</p>
        <p>最终图 SHA-256：{version.source.graph_hash}</p>
        <p>外部回执：{version.source.prompt_id}</p>
        <p>收片：{new Date(version.created_at).toLocaleString("zh-CN")}</p>
      </details>
    </article>
  );
}

export function Versions({ active = true }: { active?: boolean }) {
  const candidate = useRef<HTMLVideoElement>(null);
  const controllerRef = useRef<ReturnType<typeof createComparisonPlayback> | null>(null);
  useEffect(() => {
    if (!active) controllerRef.current?.pause();
  }, [active]);
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
        <span>选择片段与候选</span>
      </div>
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
      {!selected && <p>等待首个可审版本。请让 Agent 准备素材并提交生成，可在任务页查看进度。</p>}
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
            <ReviewPlayer
              key={selected.id}
              version={selected}
              candidate={candidate}
              controllerRef={controllerRef}
            />
            <Feedback
              version={selected}
              video={candidate}
              onPause={() => controllerRef.current?.pause()}
              onLocate={(id, time) => {
                const version = versions.find((item) => item.id === id);
                if (!version) return;
                const key = clipKey(version);
                setClip(key);
                setChoices((previous) => ({ ...previous, [key]: id }));
                window.setTimeout(() => {
                  controllerRef.current?.pause();
                  if (candidate.current) candidate.current.currentTime = time;
                }, 100);
              }}
            />
          </div>
        )}
      </div>
      <Delivery />
    </section>
  );
}
