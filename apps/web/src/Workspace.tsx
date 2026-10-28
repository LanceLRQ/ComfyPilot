import { createContext, type ReactNode, useContext, useEffect, useState } from "react";
import { z } from "zod";

export const workspaceSchema = z.object({
  project_id: z.string().uuid(),
  instance_id: z.string().uuid(),
  data: z.object({
    revision: z.number().int(),
    event_cursor: z.number().int(),
    collections: z.array(
      z.object({
        run_id: z.string(),
        phase: z.string(),
        bytes_done: z.number().optional(),
        bytes_total: z.number().optional(),
        error: z.object({ message: z.string() }).nullable().optional(),
      }),
    ),
    versions: z.array(
      z.object({
        id: z.string().uuid(),
        run_id: z.string(),
        slot: z.string(),
        kind: z.enum(["video", "image"]),
        ready: z.boolean(),
        content_hash: z.string(),
        media: z.object({
          width: z.number(),
          height: z.number(),
          frame_count: z.number().nullable(),
        }),
        checks: z.object({ status: z.string() }),
        created_at: z.string(),
        source: z.object({
          graph_hash: z.string(),
          prompt_id: z.string(),
          inputs: z.array(z.object({ slot: z.string(), asset_id: z.string() })),
          target_snapshot: z.object({ target: z.object({ id: z.string(), name: z.string() }) }),
        }),
      }),
    ),
  }),
});

type Snapshot = z.infer<typeof workspaceSchema>;
const Workspace = createContext<{ snapshot: Snapshot | null; stale: boolean }>({
  snapshot: null,
  stale: true,
});

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [stale, setStale] = useState(true);
  useEffect(() => {
    const controller = new AbortController();
    let stream: EventSource | null = null;
    let retry = 0;
    let cursor = 0;
    let instance = "";
    let project = "";
    let refreshing = false;
    let dirty = false;

    async function refresh() {
      if (refreshing) {
        dirty = true;
        return;
      }
      refreshing = true;
      try {
        do {
          dirty = false;
          const response = await fetch("/api/state", {
            cache: "no-store",
            signal: AbortSignal.any([controller.signal, AbortSignal.timeout(4000)]),
          });
          if (!response.ok) throw new Error("state_unavailable");
          const result = workspaceSchema.parse(await response.json());
          if (controller.signal.aborted) return;
          cursor = result.data.event_cursor;
          instance = result.instance_id;
          project = result.project_id;
          setSnapshot(result);
        } while (dirty);
      } finally {
        refreshing = false;
      }
    }
    function disconnected() {
      stream?.close();
      if (controller.signal.aborted) return;
      setStale(true);
      window.clearTimeout(retry);
      retry = window.setTimeout(() => void connect(), 1000);
    }
    async function connect() {
      try {
        await refresh();
        if (controller.signal.aborted) return;
        stream = new EventSource(`/api/events/stream?after=${cursor}`);
        stream.onopen = () => setStale(false);
        stream.onerror = disconnected;
        stream.addEventListener("business", (event) => {
          if (Number(event.lastEventId) <= cursor) return;
          void refresh().catch(disconnected);
        });
        stream.addEventListener("latest", (event) => {
          try {
            const result = workspaceSchema
              .pick({ project_id: true, instance_id: true })
              .extend({ data: workspaceSchema.shape.data.pick({ collections: true }) })
              .parse(JSON.parse(event.data));
            if (result.instance_id !== instance || result.project_id !== project) {
              disconnected();
              return;
            }
            setSnapshot((previous) =>
              previous
                ? { ...previous, data: { ...previous.data, collections: result.data.collections } }
                : previous,
            );
          } catch {
            disconnected();
          }
        });
      } catch {
        disconnected();
      }
    }
    void connect();
    return () => {
      controller.abort();
      stream?.close();
      window.clearTimeout(retry);
    };
  }, []);
  return <Workspace value={{ snapshot, stale }}>{children}</Workspace>;
}

export function useWorkspace() {
  return useContext(Workspace);
}
