import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { z } from "zod";
import { Assets } from "./Assets";
import { Maintenance } from "./Maintenance";
import { Monitor, ProductionSummary } from "./Monitor";
import { ProductionActivity } from "./ProductionActivity";
import { Resources } from "./Resources";
import { Settings } from "./Settings";
import { Versions } from "./Versions";
import { WorkspaceProvider } from "./Workspace";
import "./style.css";

const projectSchema = z.object({
  local_control: z.boolean().default(true),
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
const views = { review: "审片", tasks: "任务", assets: "素材", project: "项目" };
type View = keyof typeof views;

function App() {
  const [view, setView] = useState<View>("review");
  useEffect(() => {
    function navigate() {
      const value = window.location.hash.slice(1);
      setView(value === "tasks" || value === "assets" || value === "project" ? value : "review");
      window.scrollTo(0, 0);
    }
    navigate();
    window.addEventListener("hashchange", navigate);
    return () => window.removeEventListener("hashchange", navigate);
  }, []);
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

  const local = project?.local_control === true;
  const active = !local && (view === "assets" || view === "project") ? "review" : view;
  return (
    <div className="shell">
      <header className="topbar">
        <a className="brand" href="#review" aria-label="ComfyPilot 审片首页">
          <span className="brand-mark" aria-hidden="true">
            ↗
          </span>
          <span>ComfyPilot</span>
        </a>
        <span className="project-name">{project?.name ?? "正在读取项目"}</span>
        <span className="mode">
          {local ? "本机工作台" : "局域网审片"} ·{" "}
          {error ? "连接中断" : project ? "已连接" : "连接中"}
        </span>
      </header>
      <nav className="view-nav" aria-label="工作台视图">
        {Object.entries(views)
          .filter(([key]) => local || key === "review" || key === "tasks")
          .map(([key, label]) => (
            <a key={key} href={`#${key}`} aria-current={active === key ? "page" : undefined}>
              {label}
            </a>
          ))}
      </nav>
      <main>
        <h1>{views[active]}</h1>
        {error && (
          <p className="error" role="alert">
            项目连接中断，保留最后数据，正在重连。
          </p>
        )}
        <ProductionSummary />
        <div hidden={active !== "review"}>
          <Versions active={active === "review"} />
        </div>
        <div hidden={active !== "tasks"} className="workspace settings task-view">
          <ProductionActivity />
          <Monitor />
        </div>
        {local && (
          <>
            <div hidden={active !== "assets"}>
              <Assets />
            </div>
            <div hidden={active !== "project"}>
              <details className="project-context">
                <summary>项目目录与服务信息</summary>
                <p className="root-path">{project.root}</p>
                <p>
                  项目标识：<code>{project.project_id}</code>
                </p>
                <p>
                  数据格式：Schema {project.schema_version} · Worker：{project.worker.status} ·
                  PID：{project.worker.pid ?? "—"}
                </p>
                <p>
                  创建于 {new Date(project.created_at).toLocaleString("zh-CN", { hour12: false })}
                </p>
              </details>
              <Settings />
              <Resources />
              <Maintenance />
            </div>
          </>
        )}
        <footer>
          <span>
            {local ? "本机管理 · 数据保存在当前项目目录" : "局域网审片 · 管理操作请使用本机 Agent"}
          </span>
          <span>{updatedAt ? `项目连接更新于 ${updatedAt}` : "正在读取…"}</span>
        </footer>
      </main>
    </div>
  );
}

const root = document.getElementById("root");
if (root)
  createRoot(root).render(
    <WorkspaceProvider>
      <App />
    </WorkspaceProvider>,
  );
