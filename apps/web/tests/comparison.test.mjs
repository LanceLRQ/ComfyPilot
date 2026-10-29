import assert from "node:assert/strict";
import test from "node:test";
import { createComparisonPlayback } from "../src/comparison.ts";

class Video extends EventTarget {
  currentTime = 0;
  duration = 4;
  readyState = 3;
  seeking = false;
  paused = true;
  ended = false;
  reject = false;
  async play() {
    if (this.reject) throw Error("blocked");
    this.paused = false;
  }
  pause() {
    if (!this.paused) {
      this.paused = true;
      this.dispatchEvent(new Event("pause"));
    }
  }
  emit(name) {
    this.dispatchEvent(new Event(name));
  }
}

function setup(options = {}) {
  let callback;
  const previous = [globalThis.requestAnimationFrame, globalThis.cancelAnimationFrame];
  globalThis.requestAnimationFrame = (value) => {
    callback = value;
    return 1;
  };
  globalThis.cancelAnimationFrame = () => {
    callback = undefined;
  };
  const candidate = new Video();
  const original = new Video();
  let state;
  const player = createComparisonPlayback(
    candidate,
    original,
    { fps: "24/1", ...options },
    (value) => {
      state = value;
    },
  );
  return {
    candidate,
    original,
    player,
    state: () => state,
    frame: () => callback?.(),
    close() {
      player.destroy();
      [globalThis.requestAnimationFrame, globalThis.cancelAnimationFrame] = previous;
    },
  };
}
const flush = () => new Promise((resolve) => setImmediate(resolve));

test("shared playback uses the original clock, seeks both and steps using actual FPS", async () => {
  const x = setup({ fps: "30/1" });
  try {
    x.player.toggle();
    await flush();
    assert.equal(x.candidate.paused, false);
    assert.equal(x.original.paused, false);
    x.original.currentTime = 1;
    x.candidate.currentTime = 0.6;
    x.frame();
    assert.equal(x.candidate.currentTime, 1);
    x.player.seek(2);
    await flush();
    assert.equal(x.original.currentTime, 2);
    assert.equal(x.candidate.currentTime, 2);
    x.player.step(1);
    assert.equal(x.candidate.paused, true);
    assert.equal(x.original.paused, true);
    assert.ok(Math.abs(x.candidate.currentTime - 61 / 30) < 1e-9);
    x.player.step(-1);
    assert.ok(Math.abs(x.candidate.currentTime - 2) < 1e-9);
  } finally {
    x.close();
  }
});

test("either side buffering pauses both, waits for both, and resumes only while wanted", async () => {
  const x = setup();
  try {
    x.player.toggle();
    await flush();
    x.candidate.readyState = 2;
    x.candidate.emit("waiting");
    assert.equal(x.candidate.paused, true);
    assert.equal(x.original.paused, true);
    assert.equal(x.state().status, "buffering");
    x.original.emit("canplay");
    await flush();
    assert.equal(x.original.paused, true);
    x.candidate.readyState = 3;
    x.candidate.emit("canplay");
    await flush();
    assert.equal(x.candidate.paused, false);
    assert.equal(x.original.paused, false);
    x.original.readyState = 2;
    x.original.emit("stalled");
    x.player.pause();
    x.original.readyState = 3;
    x.original.emit("canplay");
    await flush();
    assert.equal(x.candidate.paused, true);
    assert.equal(x.original.paused, true);
  } finally {
    x.close();
  }
});

test("known offsets and duration limits remain in candidate seconds", () => {
  const x = setup({ offset: 1, durationLimit: 2 });
  try {
    x.player.seek(1.5);
    assert.equal(x.candidate.currentTime, 1.5);
    assert.equal(x.original.currentTime, 2.5);
    x.player.seek(99);
    assert.equal(x.candidate.currentTime, 2);
    x.player.seek(-1);
    assert.equal(x.original.currentTime, 1);
  } finally {
    x.close();
  }
});

test("feedback seeks and capture pause also control the other video", async () => {
  const x = setup({ offset: 1 });
  try {
    x.candidate.currentTime = 1.25;
    x.candidate.emit("seeked");
    assert.equal(x.original.currentTime, 2.25);
    x.player.toggle();
    await flush();
    x.candidate.pause();
    assert.equal(x.original.paused, true);
    assert.equal(x.state().playing, false);
  } finally {
    x.close();
  }
});

test("shorter video ends the pair and replay starts both at zero", async () => {
  const x = setup();
  try {
    x.original.duration = 2;
    x.original.emit("loadedmetadata");
    assert.equal(x.state().duration, 2);
    x.player.toggle();
    await flush();
    x.original.currentTime = 2;
    x.original.ended = true;
    x.frame();
    assert.equal(x.candidate.paused, true);
    assert.equal(x.original.paused, true);
    assert.equal(x.state().status, "ended");
    x.original.ended = false;
    x.player.toggle();
    await flush();
    assert.equal(x.candidate.currentTime, 0);
    assert.equal(x.original.currentTime, 0);
  } finally {
    x.close();
  }
});

test("play rejection and media errors leave both paused with a useful status", async () => {
  const x = setup();
  try {
    x.original.reject = true;
    x.player.toggle();
    await flush();
    assert.equal(x.state().status, "error");
    assert.equal(x.candidate.paused, true);
    x.original.reject = false;
    x.player.toggle();
    await flush();
    x.original.emit("error");
    assert.equal(x.candidate.paused, true);
    assert.equal(x.state().status, "error");
  } finally {
    x.close();
  }
});

test("metadata is required and missing FPS disables frame stepping", () => {
  const x = setup({ fps: null });
  try {
    x.candidate.duration = Number.NaN;
    x.candidate.emit("loadedmetadata");
    assert.equal(x.state().duration, 0);
    assert.equal(x.state().frameSeconds, 0);
    x.player.step(1);
    assert.equal(x.candidate.currentTime, 0);
  } finally {
    x.close();
  }
});

test("unmount during pending play cannot restart media or publish stale state", async () => {
  const x = setup();
  let resolve;
  x.original.play = () =>
    new Promise((done) => {
      resolve = done;
    });
  x.player.toggle();
  x.player.destroy();
  try {
    x.original.paused = false;
    resolve();
    await flush();
    assert.equal(x.original.paused, true);
    assert.equal(x.candidate.paused, true);
  } finally {
    x.close();
  }
});

test("frame stepping from a seek between frames uses the displayed frame", () => {
  const x = setup();
  try {
    x.player.seek(1.03);
    x.player.step(1);
    assert.ok(Math.abs(x.candidate.currentTime - 25 / 24) < 1e-9);
    x.player.step(-1);
    assert.ok(Math.abs(x.candidate.currentTime - 1) < 1e-9);
  } finally {
    x.close();
  }
});

test("browser microsecond seek positions do not skip a frame", () => {
  const x = setup();
  try {
    x.player.seek(1.333333);
    x.player.step(-1);
    assert.ok(Math.abs(x.candidate.currentTime - 31 / 24) < 1e-9);
  } finally {
    x.close();
  }
});

test("fractional FPS steps keep the candidate frame interval", () => {
  const x = setup({ fps: "30000/1001" });
  try {
    x.player.step(1);
    assert.ok(Math.abs(x.candidate.currentTime - 1001 / 30000) < 1e-9);
    x.player.step(1);
    assert.ok(Math.abs(x.candidate.currentTime - 2002 / 30000) < 1e-9);
  } finally {
    x.close();
  }
});
