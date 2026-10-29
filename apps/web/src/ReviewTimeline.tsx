import { useWorkspace } from "./Workspace";

export function ReviewTimeline({
  versionId,
  duration,
  time,
  onLocate,
}: {
  versionId: string;
  duration: number;
  time: number;
  onLocate: (time: number) => void;
}) {
  const { snapshot } = useWorkspace();
  const items = (snapshot?.data.feedback ?? [])
    .filter((item) => item.version_id === versionId)
    .sort((a, b) => a.start_seconds - b.start_seconds);
  const statuses = { open: "未处理", addressed: "待验证", verified: "已验证" };
  return (
    <section className="review-timeline" aria-label="当前候选反馈时间轴">
      <div className="section-heading">
        <h3>反馈时间轴</h3>
        <span>{items.length} 条 · 候选时间</span>
      </div>
      {!items.length ? (
        <p className="settings-note">尚无反馈标记。</p>
      ) : !duration ? (
        <p className="settings-note">时间轴等待媒体就绪，反馈记录仍在侧面板中。</p>
      ) : (
        <>
          <div className="timeline-scale" aria-hidden="true">
            <span>0 秒</span>
            <span>{duration.toFixed(3)} 秒</span>
          </div>
          <div className="timeline-lanes">
            {items.map((item) => (
              <button
                type="button"
                key={item.id}
                className="timeline-row"
                data-status={item.status}
                disabled={item.start_seconds > duration}
                title={item.text}
                aria-label={`定位反馈 ${item.start_seconds.toFixed(3)}–${item.end_seconds.toFixed(3)} 秒，${statuses[item.status]}：${item.text}`}
                onClick={() => onLocate(item.start_seconds)}
              >
                <span className="timeline-caption">
                  <span className="timeline-label">
                    {statuses[item.status]} · {item.start_seconds.toFixed(3)} 秒
                  </span>
                  <span className="timeline-label">
                    {item.end_seconds > duration ? "超出可对照范围 · " : ""}
                    {item.text}
                  </span>
                </span>
                <span className="timeline-track" aria-hidden="true">
                  <span
                    className="timeline-range"
                    style={{
                      left: `${Math.min(100, (item.start_seconds / duration) * 100)}%`,
                      width: `${((Math.min(duration, item.end_seconds) - Math.min(duration, item.start_seconds)) / duration) * 100}%`,
                    }}
                  />
                  <span
                    className="timeline-playhead"
                    style={{ left: `${Math.min(100, (time / duration) * 100)}%` }}
                  />
                </span>
              </button>
            ))}
          </div>
        </>
      )}
    </section>
  );
}
