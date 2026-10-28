import { createContext, type ReactNode, useContext, useEffect, useState } from "react";
import { z } from "zod";
import { monitoringSchema } from "./monitoring";

export const regionSchema = z.object({
  x: z.number(),
  y: z.number(),
  width: z.number(),
  height: z.number(),
});
export const screenshotSchema = z.object({
  id: z.string(),
  url: z.string(),
  candidate_seconds: z.number(),
});
export const feedbackSchema = z.object({
  id: z.string(),
  version_id: z.string(),
  task_id: z.string(),
  plan_id: z.string(),
  kind: z.enum(["note", "needs_changes"]),
  text: z.string(),
  status: z.enum(["open", "addressed", "verified"]),
  start_seconds: z.number(),
  end_seconds: z.number(),
  regions: z.array(regionSchema),
  screenshot: screenshotSchema.nullable(),
  source: z.string(),
  original_range: z.object({ start_seconds: z.number(), end_seconds: z.number() }).nullable(),
});

export const workspaceSchema = z.object({
  project_id: z.string().uuid(),
  instance_id: z.string().uuid(),
  data: z.object({
    monitoring: monitoringSchema,
    paused_plan_ids: z.array(z.string()),
    tasks: z.array(
      z.object({ id: z.string(), active_plan_id: z.string().nullable(), goal: z.string() }),
    ),
    plans: z.array(z.object({ id: z.string(), task_id: z.string(), status: z.string() })),
    runs: z.array(
      z.object({
        id: z.string(),
        plan_id: z.string(),
        action_index: z.number(),
        status: z.string(),
        has_intent: z.boolean(),
        started_at: z.string().nullable(),
        document: z.object({
          step: z.object({ target_id: z.string(), segment_id: z.string().optional() }).optional(),
          dispatch_blocked: z.object({ code: z.string() }).optional(),
        }),
      }),
    ),
    dispatch: z.object({
      manual_pause: z.boolean(),
      review_hold: z.boolean(),
      reconciliation_required: z.boolean(),
    }),
    activity: z.array(
      z.object({
        event_id: z.number(),
        action: z.string(),
        source: z.string(),
        created_at: z.string(),
        object_id: z.string().nullable(),
      }),
    ),
    revision: z.number().int(),
    event_cursor: z.number().int(),
    feedback: z.array(feedbackSchema),
    adoptions: z.array(
      z.object({
        task_id: z.string(),
        segment_id: z.string(),
        slot: z.string(),
        version_id: z.string(),
        decision_id: z.string(),
      }),
    ),
    decisions: z.array(
      z.object({
        id: z.string(),
        version_id: z.string(),
        decision: z.enum(["adopt", "reject"]),
        source: z.string(),
        user_statement: z.string(),
      }),
    ),
    evaluations: z.array(
      z.object({ id: z.string(), version_id: z.string(), text: z.string(), source: z.string() }),
    ),
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
        task_id: z.string(),
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
          segment_id: z.string().nullable().optional(),
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
              .extend({
                data: workspaceSchema.shape.data.pick({ collections: true, monitoring: true }),
              })
              .parse(JSON.parse(event.data));
            if (result.instance_id !== instance || result.project_id !== project) {
              disconnected();
              return;
            }
            setSnapshot((previous) =>
              previous
                ? {
                    ...previous,
                    data: {
                      ...previous.data,
                      collections: result.data.collections,
                      monitoring: result.data.monitoring,
                    },
                  }
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
