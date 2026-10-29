import { useCallback, useEffect, useRef, useState } from "react";
import { z } from "zod";
import { createComparisonPlayback, type PlaybackState } from "./comparison";
import { Decisions, Delivery } from "./Decisions";
import { Feedback } from "./Feedback";
import { ReviewTimeline } from "./ReviewTimeline";
import { SidePanel } from "./SidePanel";
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
  feedbackOpen,
  onFeedback,
  onOpenRun,
}: {
  version: Version;
  candidate: React.RefObject<HTMLVideoElement | null>;
  controllerRef: React.RefObject<ReturnType<typeof createComparisonPlayback> | null>;
  feedbackOpen: boolean;
  onFeedback: () => void;
  onOpenRun: (id: string) => void;
}) {
  const original = useRef<HTMLVideoElement>(null);
  const [mobileView, setMobileView] = useState("candidate");
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
    <article className="review-detail" data-mobile-view={mobileView}>
      {version.kind === "video" && (
        <fieldset className="mobile-review-views" aria-label="手机审片画面">
          {Object.entries({ candidate: "结果", source: "原片", both: "对照" }).map(
            ([view, label]) => (
              <button
                key={view}
                type="button"
                aria-pressed={mobileView === view}
                onClick={() => setMobileView(view)}
              >
                {label}
              </button>
            ),
          )}
        </fieldset>
      )}
      <div className="review-panes">
        <figure>
          <figcaption>候选结果</figcaption>
          {version.kind === "video" ? (
            <video
              ref={candidate}
              muted
              playsInline
              preload="auto"
              src={url}
              poster={`/api/versions/${version.id}/poster`}
              aria-label="候选结果"
            >
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
              poster={`/api/versions/${version.id}/poster?view=source`}
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
      <div className="review-tools">
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
        <Decisions version={version} feedbackOpen={feedbackOpen} onFeedback={onFeedback} />
      </div>
      {version.kind === "video" && (
        <ReviewTimeline
          versionId={version.id}
          duration={playback.duration}
          time={playback.time}
          onLocate={(time) => {
            controllerRef.current?.pause();
            controllerRef.current?.seek(time);
            onFeedback();
          }}
        />
      )}
      <button type="button" aria-haspopup="dialog" onClick={() => onOpenRun(version.run_id)}>
        查看当前候选运行详情
      </button>
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

export function Versions({
  active = true,
  onOpenRun,
}: {
  active?: boolean;
  onOpenRun: (id: string) => void;
}) {
  const focusRoot = useRef<HTMLElement>(null);
  const focusButton = useRef<HTMLButtonElement>(null);
  const [focused, setFocused] = useState(false);
  const [focusMessage, setFocusMessage] = useState("");
  const candidate = useRef<HTMLVideoElement>(null);
  const controllerRef = useRef<ReturnType<typeof createComparisonPlayback> | null>(null);
  useEffect(() => {
    if (!active) {
      controllerRef.current?.pause();
      setFocused(false);
      if (document.fullscreenElement === focusRoot.current)
        void document.exitFullscreen().catch(() => setFocusMessage("请使用浏览器退出全屏。"));
    }
  }, [active]);
  useEffect(() => {
    const root = focusRoot.current;
    if (!focused || !root) return;
    const overflow = document.body.style.overflow;
    const { scrollX, scrollY } = window;
    const blocked: { element: HTMLElement; inert: boolean }[] = [];
    for (let node: HTMLElement | null = root; node?.parentElement; node = node.parentElement) {
      for (const sibling of node.parentElement.children) {
        if (sibling !== node && sibling instanceof HTMLElement) {
          blocked.push({ element: sibling, inert: sibling.inert });
          sibling.inert = true;
        }
      }
      if (node.parentElement === document.body) break;
    }
    document.body.style.overflow = "hidden";
    function changed() {
      if (document.fullscreenElement !== root) setFocused(false);
    }
    document.addEventListener("fullscreenchange", changed);
    return () => {
      document.removeEventListener("fullscreenchange", changed);
      document.body.style.overflow = overflow;
      for (const item of blocked) item.element.inert = item.inert;
      window.scrollTo(scrollX, scrollY);
      focusButton.current?.focus({ preventScroll: true });
    };
  }, [focused]);
  function enterFocus() {
    setFocused(true);
    setFocusMessage("");
    if (focusRoot.current?.requestFullscreen)
      void focusRoot.current
        .requestFullscreen()
        .catch(() => setFocusMessage("浏览器未允许系统全屏，已使用页面专注模式。"));
    else setFocusMessage("浏览器不支持系统全屏，已使用页面专注模式。");
  }
  const exitFocus = useCallback(async () => {
    if (document.fullscreenElement === focusRoot.current) await document.exitFullscreen();
    setFocused(false);
  }, []);
  const { snapshot, stale } = useWorkspace();
  const versions = snapshot?.data.versions ?? [];
  const [clip, setClip] = useState("");
  const [choices, setChoices] = useState<Record<string, string>>({});
  const [feedbackOpen, setFeedbackOpen] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  useEffect(() => {
    if (!focused) return;
    function dismiss(event: KeyboardEvent) {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      event.preventDefault();
      if (feedbackOpen) setFeedbackOpen(false);
      else void exitFocus().catch(() => setFocusMessage("请使用浏览器退出全屏。"));
    }
    document.addEventListener("keydown", dismiss);
    return () => document.removeEventListener("keydown", dismiss);
  }, [focused, feedbackOpen, exitFocus]);
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
    <section
      ref={focusRoot}
      className="workspace settings review-workbench"
      data-focus={focused}
      aria-labelledby="versions-title"
    >
      <div className="section-heading">
        <h2 id="versions-title">逐段审片</h2>
        <span>选择片段与候选</span>
      </div>
      {focusMessage && <p role="status">{focusMessage}</p>}
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
              片段 {Array.from(groups.keys()).indexOf(key) + 1}
              <small>
                {items.length} 个候选 · {items[0].slot}
              </small>
            </button>
          ))}
        </nav>
        {selected && (
          <div className="review-stage">
            <div className="candidate-tools">
              <details
                className="candidate-picker"
                open={pickerOpen}
                onToggle={(event) => setPickerOpen(event.currentTarget.open)}
              >
                <summary>
                  审阅候选 · {selected.id.slice(0, 8)} · {candidates.length} 个候选
                </summary>
                <fieldset className="candidate-list" aria-label="候选缩略图">
                  {[...candidates].reverse().map((item, index) => {
                    const adopted = snapshot?.data.adoptions.some(
                      (entry) => entry.version_id === item.id,
                    );
                    const pending =
                      snapshot?.data.feedback.filter(
                        (entry) =>
                          entry.version_id === item.id &&
                          entry.kind === "needs_changes" &&
                          entry.status !== "verified",
                      ).length ?? 0;
                    const rejected =
                      snapshot?.data.decisions.find((entry) => entry.version_id === item.id)
                        ?.decision === "reject";
                    return (
                      <button
                        key={item.id}
                        type="button"
                        aria-label={`审阅候选 ${item.id.slice(0, 8)}`}
                        aria-pressed={selected.id === item.id}
                        onClick={() => {
                          setChoices((previous) => ({ ...previous, [activeClip]: item.id }));
                          if (window.matchMedia("(max-width: 800px)").matches) setPickerOpen(false);
                        }}
                      >
                        <span className="candidate-image">
                          <img
                            loading="lazy"
                            src={`/api/versions/${item.id}/poster`}
                            alt=""
                            onError={(event) => {
                              event.currentTarget.hidden = true;
                            }}
                          />
                          <span>预览不可用</span>
                        </span>
                        <span className="candidate-caption">
                          <strong>
                            候选 {index + 1} · {item.id.slice(0, 8)}
                          </strong>
                          <span>
                            {new Date(item.created_at).toLocaleTimeString("zh-CN", {
                              hour12: false,
                            })}{" "}
                            · {item.source.target_snapshot.target.name}
                          </span>
                          <small>
                            检查{item.checks.status === "passed" ? "通过" : item.checks.status} ·{" "}
                            {adopted ? "用户已采用" : rejected ? "用户已拒绝" : "尚未采用"}
                            {pending ? ` · ${pending} 条待复核` : ""}
                          </small>
                        </span>
                      </button>
                    );
                  })}
                </fieldset>
              </details>
              {selected.kind === "video" && (
                <button
                  type="button"
                  ref={focusButton}
                  aria-pressed={focused}
                  onClick={() => {
                    if (focused)
                      void exitFocus().catch(() => setFocusMessage("请使用浏览器退出全屏。"));
                    else enterFocus();
                  }}
                >
                  {focused ? "退出全屏审片" : "全屏审片"}
                </button>
              )}
            </div>
            <ReviewPlayer
              key={selected.id}
              version={selected}
              candidate={candidate}
              controllerRef={controllerRef}
              feedbackOpen={feedbackOpen}
              onFeedback={() => setFeedbackOpen(true)}
              onOpenRun={(id) => {
                setFeedbackOpen(false);
                void exitFocus()
                  .then(() => onOpenRun(id))
                  .catch(() => setFocusMessage("请先退出全屏再查看运行详情。"));
              }}
            />
          </div>
        )}
        {selected && (
          <SidePanel
            id="feedback-panel"
            title="审片反馈"
            open={active && feedbackOpen}
            dock
            onClose={() => setFeedbackOpen(false)}
          >
            <p className="settings-note">
              候选 {selected.id.slice(0, 8)} · 关闭面板保留草稿；截图或定位反馈会暂停两路播放。
            </p>
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
          </SidePanel>
        )}
      </div>
      <Delivery />
    </section>
  );
}
