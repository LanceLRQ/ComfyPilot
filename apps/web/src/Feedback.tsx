import { type RefObject, useEffect, useRef, useState } from "react";
import { z } from "zod";
import { randomUUID } from "./uuid";
import type { Version } from "./Versions";
import { type regionSchema, screenshotSchema, useWorkspace } from "./Workspace";

type Draft = {
  text: string;
  start: number;
  end: number;
  command_id?: string;
  revision?: number;
  screenshot?: z.infer<typeof screenshotSchema>;
  regions: z.infer<typeof regionSchema>[];
};
const reply = z.object({ error: z.object({ code: z.string(), message: z.string() }).optional() });

export function Feedback({
  version,
  video,
  onPause,
  onLocate,
}: {
  version: Version;
  video: RefObject<HTMLVideoElement | null>;
  onPause: () => void;
  onLocate: (version: string, time: number) => void;
}) {
  const { snapshot, stale } = useWorkspace();
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [inspected, setInspected] = useState("");
  const canvas = useRef<HTMLCanvasElement>(null);
  const point = useRef<{ x: number; y: number } | null>(null);
  const draft = drafts[version.id] ?? { text: "", start: 0, end: 0, regions: [] };
  const screenshot = draft.screenshot;
  useEffect(() => {
    if (!screenshot) return;
    const image = new Image();
    image.onload = () => {
      if (canvas.current) {
        canvas.current.width = image.width;
        canvas.current.height = image.height;
        canvas.current.getContext("2d")?.drawImage(image, 0, 0);
      }
    };
    image.src = screenshot.url;
    return () => {
      image.onload = null;
    };
  }, [screenshot]);
  function change(fields: Partial<Draft>) {
    setDrafts((previous) => ({
      ...previous,
      [version.id]: {
        ...draft,
        ...fields,
        revision: fields.revision ?? draft.revision ?? snapshot?.data.revision,
        command_id: randomUUID(),
      },
    }));
  }
  async function capture() {
    if (!video.current || !canvas.current || !snapshot || video.current.readyState < 2) return;
    const player = video.current;
    onPause();
    player.pause();
    const time = player.currentTime;
    const frame = canvas.current;
    const scale = Math.min(1, 4096 / Math.max(player.videoWidth, player.videoHeight));
    frame.width = Math.round(player.videoWidth * scale);
    frame.height = Math.round(player.videoHeight * scale);
    frame.getContext("2d")?.drawImage(player, 0, 0, frame.width, frame.height);
    setBusy(true);
    try {
      const blob = await new Promise<Blob | null>((resolve) => frame.toBlob(resolve, "image/png"));
      if (!blob) throw new Error("无法截取当前画面。");
      const response = await fetch(
        `/api/review/screenshots?version_id=${version.id}&candidate_seconds=${time}`,
        {
          method: "POST",
          headers: { "X-ComfyPilot-Instance": snapshot.instance_id },
          body: blob,
          signal: AbortSignal.timeout(10000),
        },
      );
      const body = await response.json();
      if (!response.ok) throw new Error(reply.parse(body).error?.message ?? "截图保存失败。");
      const saved = z.object({ data: screenshotSchema }).parse(body).data;
      change({ screenshot: saved, start: time, end: time, regions: [] });
      setMessage("截图已保存，可在画面上拖拽圈选。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "截图保存失败。");
    } finally {
      setBusy(false);
    }
  }
  async function save(kind: "note" | "needs_changes") {
    if (!snapshot || !draft.text.trim()) return;
    setBusy(true);
    setConflict(false);
    try {
      const response = await fetch("/api/execution/command", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-ComfyPilot-Instance": snapshot.instance_id,
        },
        signal: AbortSignal.timeout(8000),
        body: JSON.stringify({
          action: "save_feedback",
          source: "web_user",
          command_id: draft.command_id,
          expected_revision: draft.revision,
          version_id: version.id,
          kind,
          text: draft.text,
          start_seconds: draft.start,
          end_seconds: draft.end,
          screenshot_id: screenshot?.id ?? null,
          regions: draft.regions,
        }),
      });
      const body = reply.parse(await response.json());
      if (!response.ok) {
        setConflict(body.error?.code === "conflict");
        throw new Error(body.error?.message ?? "保存失败，请保留草稿重试。");
      }
      setDrafts((previous) => ({
        ...previous,
        [version.id]: { text: "", start: 0, end: 0, regions: [] },
      }));
      setMessage(
        kind === "needs_changes"
          ? "反馈已保存；若关联当前有效方案，该任务的后续派发已挂起。"
          : "备注已保存。",
      );
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "保存失败；草稿与命令ID已保留，可重试。");
    } finally {
      setBusy(false);
    }
  }
  const feedback = snapshot?.data.feedback.filter((item) => item.version_id === version.id) ?? [];
  const inspection = feedback.find((item) => item.id === inspected);
  async function verify(id: string) {
    if (!snapshot) return;
    setBusy(true);
    try {
      const response = await fetch("/api/execution/command", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-ComfyPilot-Instance": snapshot.instance_id,
        },
        signal: AbortSignal.timeout(8000),
        body: JSON.stringify({
          action: "update_feedback",
          source: "web_user",
          command_id: randomUUID(),
          expected_revision: snapshot.data.revision,
          feedback_id: id,
          status: "verified",
          note: "用户在网页确认此反馈已经解决。",
        }),
      });
      if (!response.ok)
        throw new Error(reply.parse(await response.json()).error?.message ?? "验证未保存。");
      setMessage("用户验证已保存。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "验证未保存。");
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="review-feedback" aria-labelledby="feedback-title">
      <h3 id="feedback-title">意见与截图</h3>
      <p className="settings-note">
        “需要修改”会阻止关联任务的后续派发，已经提交的运行继续对账和收片。圈选坐标相对截图画面。
      </p>
      <label>
        修改意见或备注
        <textarea
          aria-label="修改意见或备注"
          value={draft.text}
          maxLength={4000}
          onChange={(event) => change({ text: event.target.value })}
        />
      </label>
      <div className="resource-actions">
        <label>
          候选起点（秒）
          <input
            type="number"
            min="0"
            step="0.001"
            value={draft.start}
            onChange={(event) => change({ start: Number(event.target.value) })}
          />
        </label>
        <label>
          候选终点（秒）
          <input
            type="number"
            min="0"
            step="0.001"
            value={draft.end}
            onChange={(event) => change({ end: Number(event.target.value) })}
          />
        </label>
        <button
          type="button"
          disabled={busy || stale || version.kind !== "video"}
          onClick={() => void capture()}
        >
          截取当前画面
        </button>
      </div>
      <div className="annotation-frame" hidden={!screenshot}>
        <canvas
          ref={canvas}
          aria-label="反馈截图，可拖拽圈选"
          onPointerDown={(event) => {
            event.currentTarget.setPointerCapture(event.pointerId);
            const box = event.currentTarget.getBoundingClientRect();
            point.current = {
              x: (event.clientX - box.left) / box.width,
              y: (event.clientY - box.top) / box.height,
            };
          }}
          onPointerUp={(event) => {
            const start = point.current;
            point.current = null;
            if (!start) return;
            const box = event.currentTarget.getBoundingClientRect();
            const x = Math.min(1, Math.max(0, (event.clientX - box.left) / box.width));
            const y = Math.min(1, Math.max(0, (event.clientY - box.top) / box.height));
            const region = {
              x: Math.min(x, start.x),
              y: Math.min(y, start.y),
              width: Math.abs(x - start.x),
              height: Math.abs(y - start.y),
            };
            if (region.width > 0 && region.height > 0)
              change({
                regions: Array.from(
                  new Map(
                    [...draft.regions, region].map((item) => [JSON.stringify(item), item]),
                  ).values(),
                ).slice(-10),
              });
          }}
        />
        <svg viewBox="0 0 1 1" preserveAspectRatio="none" aria-hidden="true">
          {draft.regions.map((region) => (
            <rect
              key={JSON.stringify(region)}
              x={region.x}
              y={region.y}
              width={region.width}
              height={region.height}
            />
          ))}
        </svg>
      </div>
      {screenshot && (
        <button type="button" onClick={() => change({ regions: [] })}>
          清除圈选
        </button>
      )}
      <div className="resource-actions">
        <button
          type="button"
          disabled={busy || stale || !draft.text.trim()}
          onClick={() => void save("needs_changes")}
        >
          需要修改
        </button>
        <button
          type="button"
          disabled={busy || stale || !draft.text.trim()}
          onClick={() => void save("note")}
        >
          保存普通备注
        </button>
      </div>
      {message && <p role="status">{message}</p>}
      {conflict && (
        <button
          type="button"
          onClick={() => {
            change({ revision: snapshot?.data.revision });
            setConflict(false);
            setMessage("草稿保留，已采用当前修订，请核对后重新提交。");
          }}
        >
          保留草稿，采用当前修订
        </button>
      )}
      <h3 className="feedback-history-title">此候选的反馈历史（{feedback.length}）</h3>
      {!feedback.length && <p className="settings-note">尚无反馈，草稿只在提交后保存为记录。</p>}
      <ul className="feedback-items">
        {feedback.map((item) => (
          <li key={item.id}>
            <button
              type="button"
              onClick={() => {
                setInspected(item.id);
                onLocate(item.version_id, item.start_seconds);
              }}
            >
              {item.kind === "needs_changes" ? "需要修改" : "备注"} ·{" "}
              {item.start_seconds.toFixed(3)}–{item.end_seconds.toFixed(3)} 秒
            </button>
            <p>{item.text}</p>
            <small>
              {
                { open: "未处理", addressed: "Agent 已处理，待用户验证", verified: "用户已验证" }[
                  item.status
                ]
              }{" "}
              · 来源 {item.source}
              {item.original_range &&
                ` · 原片 ${item.original_range.start_seconds.toFixed(3)}–${item.original_range.end_seconds.toFixed(3)} 秒`}
            </small>
            {item.screenshot && (
              <a href={item.screenshot.url}>查看反馈截图（{item.regions.length} 处圈选）</a>
            )}
            {item.status !== "verified" && (
              <button type="button" disabled={busy || stale} onClick={() => void verify(item.id)}>
                确认此反馈已解决
              </button>
            )}
          </li>
        ))}
      </ul>
      {inspection?.screenshot && (
        <div className="annotation-frame">
          <img src={inspection.screenshot.url} alt="已保存的反馈截图" />
          <svg viewBox="0 0 1 1" preserveAspectRatio="none" aria-hidden="true">
            {inspection.regions.map((region) => (
              <rect key={JSON.stringify(region)} {...region} />
            ))}
          </svg>
        </div>
      )}
    </section>
  );
}
