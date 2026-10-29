export type PlaybackState = {
  time: number;
  duration: number;
  frameSeconds: number;
  playing: boolean;
  status: "loading" | "paused" | "playing" | "buffering" | "ended" | "error";
  message: string;
};

export function createComparisonPlayback(
  candidate: HTMLVideoElement,
  original: HTMLVideoElement | null,
  options: { offset?: number; durationLimit?: number; fps?: string | null },
  onChange: (state: PlaybackState) => void,
) {
  const videos = original ? [candidate, original] : [candidate];
  const offset = options.offset ?? 0;
  const [numerator, denominator = "1"] = (options.fps ?? "").split("/");
  const fps = Number(numerator) / Number(denominator);
  const frameSeconds = Number.isFinite(fps) && fps > 0 ? 1 / fps : 0;
  const events = new AbortController();
  let wanted = false;
  let buffering = false;
  let starting = false;
  let disposed = false;
  let frame = 0;
  let attempt = 0;
  let status: PlaybackState["status"] = "loading";
  let message = "正在加载视频…";

  function duration() {
    if (videos.some((video) => !Number.isFinite(video.duration) || video.duration <= 0)) return 0;
    return Math.max(
      0,
      Math.min(
        candidate.duration,
        original ? original.duration - offset : Infinity,
        options.durationLimit ?? Infinity,
      ),
    );
  }
  function time() {
    return Math.max(
      0,
      Math.min(duration(), original ? original.currentTime - offset : candidate.currentTime),
    );
  }
  function publish() {
    if (!disposed)
      onChange({
        time: time(),
        duration: duration(),
        frameSeconds,
        playing: wanted,
        status,
        message,
      });
  }
  function pauseBoth() {
    cancelAnimationFrame(frame);
    videos.forEach((video) => {
      video.pause();
    });
  }
  function pause() {
    wanted = buffering = starting = false;
    attempt++;
    pauseBoth();
    status = "paused";
    message = "已暂停";
    publish();
  }
  function position(value: number) {
    const target = Math.max(0, Math.min(duration(), value));
    if (Math.abs(candidate.currentTime - target) > 0.000001) candidate.currentTime = target;
    if (original && Math.abs(original.currentTime - target - offset) > 0.000001)
      original.currentTime = target + offset;
  }
  function finish() {
    if (disposed || (status === "ended" && !wanted)) return;
    pause();
    position(duration());
    status = "ended";
    message = "本段已结束，可重播或逐帧查看";
    publish();
  }
  function tick() {
    if (disposed || !wanted || buffering) return;
    if (videos.some((video) => video.ended) || time() >= duration() - 0.015) {
      finish();
      return;
    }
    if (original && Math.abs(candidate.currentTime - time()) > 0.08) candidate.currentTime = time();
    publish();
    frame = requestAnimationFrame(tick);
  }
  async function resume() {
    if (disposed || !wanted || starting) return;
    if (!duration() || videos.some((video) => video.readyState < 3 || video.seeking)) {
      buffering = true;
      status = "buffering";
      message = "正在缓冲，准备好后自动继续";
      publish();
      return;
    }
    buffering = false;
    starting = true;
    const ticket = ++attempt;
    const results = await Promise.allSettled(videos.map((video) => video.play()));
    if (disposed || !wanted || ticket !== attempt) {
      if (disposed || !wanted) pauseBoth();
      return;
    }
    starting = false;
    if (results.some((result) => result.status === "rejected")) {
      pause();
      status = "error";
      message = "浏览器未能播放视频，请点击播放重试";
    } else {
      status = "playing";
      message = original ? "按时间对照播放 · 静音" : "播放中 · 静音";
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(tick);
    }
    publish();
  }
  function seek(value: number) {
    buffering = wanted;
    starting = false;
    attempt++;
    pauseBoth();
    position(value);
    status = wanted ? "buffering" : "paused";
    message = wanted ? "正在定位…" : "已暂停";
    publish();
    if (wanted) void resume();
  }
  function wait() {
    if (disposed || !wanted) return;
    buffering = true;
    starting = false;
    attempt++;
    pauseBoth();
    status = "buffering";
    message = "正在缓冲，准备好后自动继续";
    publish();
    if (videos.every((video) => video.readyState >= 3 && !video.seeking)) void resume();
  }
  videos.forEach((video) => {
    const listen = (event: string, handler: () => void) =>
      video.addEventListener(event, handler, { signal: events.signal });
    listen("loadedmetadata", () => {
      if (duration() && !wanted) {
        position(candidate.currentTime);
        status = "paused";
        message = "对照已就绪 · 静音";
      }
      publish();
      if (wanted) void resume();
    });
    listen("canplay", () => {
      if (wanted) void resume();
    });
    listen("waiting", wait);
    listen("stalled", wait);
    listen("seeking", wait);
    listen("seeked", () => {
      if (
        video === candidate &&
        original &&
        Math.abs(original.currentTime - candidate.currentTime - offset) > 0.08
      )
        original.currentTime = candidate.currentTime + offset;
      if (video === original && Math.abs(candidate.currentTime - time()) > 0.08)
        candidate.currentTime = time();
      publish();
      if (wanted) void resume();
    });
    listen("pause", () => {
      if (wanted && !buffering && !starting && video.paused && !video.ended) pause();
    });
    listen("ended", finish);
    listen("error", () => {
      pause();
      status = "error";
      message = `${video === candidate ? "渲染片" : "原片参考"}加载失败，请切换候选后重试`;
      publish();
    });
  });
  position(candidate.currentTime);
  if (duration()) {
    status = "paused";
    message = "对照已就绪 · 静音";
  }
  publish();
  return {
    pause,
    seek,
    toggle() {
      if (wanted) pause();
      else {
        wanted = true;
        seek(time() >= duration() - 0.015 ? 0 : time());
      }
    },
    step(delta: number) {
      pause();
      // currentTime 的微秒截断不能让帧边界被误算到前一帧。
      if (frameSeconds)
        seek(
          (Math.floor((candidate.currentTime + 0.000001) / frameSeconds) + delta) * frameSeconds,
        );
    },
    destroy() {
      disposed = true;
      wanted = false;
      attempt++;
      events.abort();
      pauseBoth();
    },
  };
}
