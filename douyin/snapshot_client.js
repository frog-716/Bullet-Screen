(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.BulletScreenSnapshot = factory();
})(typeof self !== "undefined" ? self : this, function () {
  const identityOf = (value) => [value?.roomId, value?.sessionId, value?.runId, value?.generation].join("|");
  class SnapshotClient {
    constructor(fetchSnapshot, onSnapshot, onError) { this.fetchSnapshot = fetchSnapshot; this.onSnapshot = onSnapshot; this.onError = onError || (() => {}); this.requestGeneration = 0; this.controller = null; }
    refresh() {
      const requestGeneration = ++this.requestGeneration;
      if (this.controller) this.controller.abort();
      this.controller = typeof AbortController === "function" ? new AbortController() : null;
      const signal = this.controller ? this.controller.signal : undefined;
      return Promise.resolve(this.fetchSnapshot({ signal, requestGeneration })).then((snapshot) => { if (requestGeneration !== this.requestGeneration) return snapshot; this.onSnapshot(snapshot, requestGeneration); return snapshot; }).catch((error) => { if (requestGeneration !== this.requestGeneration || error?.name === "AbortError") return null; this.onError(error, requestGeneration); return null; });
    }
  }
  function applySnapshotState(previous, snapshot) {
    const next = { ...previous }, identityChanged = identityOf(previous) !== identityOf({ roomId: snapshot?.room_id, sessionId: snapshot?.session_id, runId: snapshot?.run_id, generation: snapshot?.generation });
    next.roomId = snapshot?.room_id ?? previous.roomId ?? ""; next.roomTitle = snapshot?.room_title ?? previous.roomTitle ?? "待连接直播间"; next.sessionId = snapshot?.session_id ?? null; next.runId = snapshot?.run_id ?? null; next.generation = snapshot?.generation ?? 0; next.status = snapshot?.status ?? "idle"; next.connected = snapshot?.status === "connected"; next.workerAlive = !!snapshot?.worker_alive; next.dataSource = snapshot?.data_source ?? "none"; next.asOf = snapshot?.as_of ?? null; next.lastValidAt = snapshot?.last_valid_at ?? null; next.freshness = snapshot?.freshness ?? null; next.coverage = snapshot?.coverage ?? { coverage_state: "unknown" }; next.metrics = snapshot?.metrics ?? null; next.events = Array.isArray(snapshot?.events?.items) ? snapshot.events.items : []; next.lastError = snapshot?.error ?? ""; next.diagnostics = snapshot?.diagnostics ?? {}; next.identityChanged = identityChanged; return next;
  }
  function clearSnapshotState(previous, error) { return { ...previous, connected: false, metrics: null, events: [], lastError: String(error || "snapshot failed") }; }
  function resetViewState(previous) { return { ...previous, connected: false, sessionId: null, runId: null, generation: 0, status: "idle", metrics: null, events: [], coverage: { coverage_state: "unknown" }, lastError: "" }; }
  const statusText = (status) => ({ connecting: "正在连接", authenticating: "正在验证连接", connected: "采集正常", stopping: "正在停止，暂时不能重新启动", stale: "服务还活着，但最近没有可靠数据", stopped: "已停止", error: "采集出错", offline: "服务离线", idle: "等待连接" }[status] || "状态未知");
  const coverageText = (coverage) => ({ reliable_with_data: "采集可靠 · 有数据", reliable_no_events: "采集可靠 · 暂无事件", gap: "存在采集缺口", unknown: "数据完整性未知" }[coverage?.coverage_state] || "数据完整性未知");
  return { SnapshotClient, applySnapshotState, clearSnapshotState, resetViewState, statusText, coverageText };
});
