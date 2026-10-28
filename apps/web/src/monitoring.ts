import { z } from "zod";

export const metric = z.object({
  value: z.unknown(),
  source: z.string(),
  unit: z.string().nullable(),
  availability: z.enum(["fresh", "stale", "unknown", "unsupported"]),
  observed_at: z.string().nullable(),
  expires_at: z.string().nullable(),
  reason: z.string().nullable(),
});
export const target = z.object({
  target_id: z.string(),
  enabled: z.boolean(),
  connection: metric,
  websocket: metric,
  metrics: z.record(z.string(), metric),
  devices: z.array(z.record(z.string(), metric)).nullable(),
  node_progress: metric.extend({
    value: z.object({ value: z.number(), max: z.number() }).nullable(),
  }),
  stage: metric.extend({
    value: z
      .object({
        phase: z.enum(["executing", "execution_finished", "execution_error"]),
        node: z.string().nullable(),
      })
      .nullable(),
  }),
  stage_elapsed_seconds: z.number().nullable(),
  capabilities: z.record(z.string(), z.object({ status: z.string(), reason: z.string() })),
});
export const monitoringSchema = z.object({
  worker: z.object({
    status: z.string(),
    pid: z.number().nullable(),
    last_seen_at: z.string().nullable(),
  }),
  platform: z.record(z.string(), metric),
  targets: z.array(target),
});
