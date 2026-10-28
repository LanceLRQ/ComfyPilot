import { useWorkspace } from "./Workspace";

export function Versions() {
  const { snapshot, stale: error } = useWorkspace();
  const versions = snapshot?.data.versions ?? [];
  const collections = snapshot?.data.collections ?? [];
  const phases: Record<string, string> = {
    waiting_output: "等待输出",
    transferring: "传输中",
    checking: "校验中",
    proxy: "制作播放代理",
    registering: "登记版本",
    blocked: "已阻塞",
    retry_wait: "等待退避重试",
    failed: "检查失败",
  };
  return (
    <section className="settings-card" aria-labelledby="versions-title">
      <div className="section-heading">
        <h2 id="versions-title">收片与预览</h2>
        <span>REVIEW READY</span>
      </div>
      <p className="settings-note">完整解码及配方规格检查通过后，版本会自动出现在这里。</p>
      {error && (
        <p role="alert" className="error">
          收片状态暂不可用，保留已有预览。
        </p>
      )}
      {!versions.length && <p>等待首个可审版本。</p>}
      {collections.map((item) => (
        <p key={item.run_id} role="status">
          {item.run_id.slice(0, 8)} · {phases[item.phase] ?? item.phase}
          {item.bytes_done !== undefined &&
            ` · 已传 ${item.bytes_done} / ${item.bytes_total ?? "未知"} 字节`}
          {item.error && ` · ${item.error.message}`}
        </p>
      ))}
      <div className="media-grid">
        {versions
          .filter((item) => item.ready)
          .map((item) => {
            const url = `/api/versions/${item.id}/content`;
            return (
              <article key={item.id} className="resource-card">
                <h3>
                  {item.slot} · {item.run_id.slice(0, 8)}
                </h3>
                <p>
                  {item.media.width} × {item.media.height} · {item.media.frame_count ?? "—"} 帧 ·
                  检查通过
                </p>
                {item.kind === "video" ? (
                  <video controls preload="metadata" src={url} aria-label="生成结果预览">
                    <track kind="captions" />
                  </video>
                ) : (
                  <img src={url} alt="生成结果预览" />
                )}
                <a href={`${url}?variant=original`}>查看原始结果</a>
              </article>
            );
          })}
      </div>
    </section>
  );
}
