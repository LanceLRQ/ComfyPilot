import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import test from "node:test";
import { randomUUID } from "../src/uuid.ts";

test("LAN HTTP without crypto.randomUUID still produces distinct UUID v4 command IDs", () => {
  const original = Object.getOwnPropertyDescriptor(globalThis, "crypto");
  Object.defineProperty(globalThis, "crypto", {
    configurable: true,
    value: { getRandomValues: webcrypto.getRandomValues.bind(webcrypto) },
  });
  try {
    const ids = Array.from({ length: 1000 }, () => randomUUID());
    assert.equal(new Set(ids).size, ids.length);
    for (const id of ids)
      assert.match(id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  } finally {
    Object.defineProperty(globalThis, "crypto", original);
  }
});
