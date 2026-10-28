import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { z } from "zod";
import { Assets } from "./Assets";
import { Monitor } from "./Monitor";
import { Resources } from "./Resources";
import { Settings } from "./Settings";
import "./style.css";

const projectSchema = z.object({
  project_id: z.string().uuid(),
  root: z.string(),
  name: z.string(),
  created_at: z.string(),
  schema_version: z.number().int(),
  worker: z.object({
    status: z.enum(["not_started", "running", "stale", "stopped"]),
    pid: z.number().int().nullable(),
    last_seen_at: z.string().nullable(),
  }),
});

type Project = z.infer<typeof projectSchema>;
const workerLabels = {
  not_started: "等待启动",
  running: "运行中",
  stale: "心跳已过期",
  stopped: "已停止",
};

function App() {
  const [project, setProject] = useState<Project | null>(null);
  const [error, setError] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    async function update() {
      try {
        const response = await fetch("/api/project", {
          signal: AbortSignal.any([controller.signal, AbortSignal.timeout(4000)]),
          cache: "no-store",
        });
        if (!response.ok) throw new Error("project_unavailable");
        const data = projectSchema.parse(await response.json());
        if (controller.signal.aborted) return;
        setProject(data);
        setError(false);
        setUpdatedAt(new Date().toLocaleTimeString("zh-CN", { hour12: false }));
      } catch {
        if (!controller.signal.aborted) setError(true);
      }
    }
    void update();
    const timer = window.setInterval(() => void update(), 5000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, []);

  const worker = project?.worker;
  return (
    <div className="shell">
      <header className="topbar">
        <a className="brand" href="/" aria-label="ComfyPilot 项目首页">
          <span className="brand-mark" aria-hidden="true">
            ↗
          </span>
          <span>ComfyPilot</span>
        </a>
        <span className="mode">
          本机工作台
          <span className="mode-dot" />
        </span>
      </header>
      <main>
        <div className="eyebrow">
          <span>WORKSPACE</span>
          <span>项目概览 / 01</span>
        </div>
        <section className="intro">
          <div>
            <p className="kicker">每一次创作，都从这里开始。</p>
            <h1>
              {project?.name ?? "正在读取项目"}
              <span className="title-dot">.</span>
            </h1>
            <p className="intro-copy">
              一个明确的工作目录，承载你与 Agent 的创作。
              <br />
              配置、资源和运行状态，都属于这个项目。
            </p>
          </div>
          <div className={`connection ${error ? "unavailable" : ""}`} role="status">
            <span className="status-light" />
            <div>
              <strong>
                {error ? "服务连接中断" : project ? "项目服务已连接" : "正在连接本机服务"}
              </strong>
              <span className="connection-note">
                {error ? "保留最后一次读取的数据" : "回环连接 · 无需登录"}
              </span>
            </div>
          </div>
        </section>
        {error && (
          <p className="error" role="alert">
            暂时无法读取项目，请检查运行服务的终端。连接恢复后会自动更新。
          </p>
        )}
        <section className="workspace" aria-labelledby="workspace-title">
          <div className="section-heading">
            <h2 id="workspace-title">当前工作目录</h2>
            <span>PROJECT ROOT</span>
          </div>
          <div className="root-path">{project?.root ?? "等待项目数据…"}</div>
          <div className="project-details">
            <div className="project-detail">
              <span className="label">项目标识</span>
              <code>{project?.project_id ?? "—"}</code>
            </div>
            <div className="project-detail">
              <span className="label">创建时间</span>
              <span className="detail-value">
                {project
                  ? new Date(project.created_at).toLocaleString("zh-CN", { hour12: false })
                  : "—"}
              </span>
            </div>
            <div className="project-detail">
              <span className="label">数据格式</span>
              <span className="detail-value">
                {project ? `Schema ${project.schema_version}` : "—"}
              </span>
            </div>
          </div>
        </section>
        <section className="lower-grid">
          <article className="worker-card">
            <div className="section-heading">
              <h2>后台 Worker</h2>
              <span>BACKGROUND PROCESS</span>
            </div>
            <div className="worker-state">
              <span
                className={`worker-orbit ${worker?.status === "running" && !error ? "active" : ""}`}
                aria-hidden="true"
              >
                ◉
              </span>
              <div>
                <strong>{worker ? workerLabels[worker.status] : "等待状态"}</strong>
                <p>独立进程，定期保存项目心跳。</p>
              </div>
            </div>
            <dl>
              <div className="worker-detail">
                <dt>进程 PID</dt>
                <dd>{worker?.pid ?? "—"}</dd>
              </div>
              <div className="worker-detail">
                <dt>最近心跳</dt>
                <dd>
                  {worker?.last_seen_at
                    ? new Date(worker.last_seen_at).toLocaleTimeString("zh-CN", { hour12: false })
                    : "—"}
                </dd>
              </div>
            </dl>
          </article>
          <article className="next-card">
            <span className="eyebrow">从项目开始，逐步创作</span>
            <h2>
              给协作一个
              <br />
              <em>确定的位置。</em>
            </h2>
            <p>已支持项目配置、多个执行端与基础监测。生成与审片将在后续阶段接入。</p>
            <span className="stage">当前阶段：项目与连接</span>
          </article>
        </section>
        <Monitor />
        <Settings />
        <Resources />
        <Assets />
        <footer>
          <span>仅本机访问 · 所有项目数据保存在当前目录</span>
          <span>{updatedAt ? `更新于 ${updatedAt} · 每 5 秒刷新` : "正在读取…"}</span>
        </footer>
      </main>
    </div>
  );
}

const root = document.getElementById("root");
if (root) createRoot(root).render(<App />);
