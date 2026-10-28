import { useWorkspace } from "./Workspace";

const states: Record<string, string> = {
  queued: "等待派发",
  cancelled: "已取消",
  dispatching: "准备提交",
  submitted: "已提交",
  running: "生成中",
  reconciling: "核对回执",
  collecting: "收片中",
  succeeded: "已收片",
  failed: "失败",
};

export function ProductionActivity() {
  const { snapshot } = useWorkspace();
  const data = snapshot?.data;
  return (
    <section className="review-feedback" aria-labelledby="production-title">
      <div className="section-heading">
        <h3 id="production-title">队列与活动</h3>
        <span>项目内运行</span>
      </div>
      <p className="settings-note">
        {data?.dispatch.manual_pause ? "已手动暂停后续派发" : "手动派发开关已打开"} ·{" "}
        {data?.dispatch.review_hold ? "部分任务等待审片处理" : "无审片挂起"} ·{" "}
        {data?.dispatch.reconciliation_required ? "存在待核对提交" : "无待核对提交"}
      </p>
      <ul className="feedback-items">
        {data?.runs.map((run) => (
          <li key={run.id}>
            <strong>
              {run.id.slice(0, 8)} · 步骤 {run.action_index + 1} ·{" "}
              {states[run.status] ?? run.status}
            </strong>
            <span>
              目标 {run.document.step?.target_id ?? "未知"} · 片段{" "}
              {run.document.step?.segment_id?.slice(0, 8) ?? "未声明"}
            </span>
            {run.document.dispatch_blocked && <span>{run.document.dispatch_blocked.code}</span>}
          </li>
        ))}
      </ul>
      <details>
        <summary>最近业务活动</summary>
        <ol className="feedback-items">
          {data?.activity.map((item) => (
            <li key={item.event_id}>
              <span>
                {new Date(item.created_at).toLocaleTimeString("zh-CN")} · {item.action} ·{" "}
                {item.source}
              </span>
              <small>
                对象 {item.object_id ?? "项目"} · 事件 {item.event_id}
              </small>
            </li>
          ))}
        </ol>
      </details>
    </section>
  );
}
