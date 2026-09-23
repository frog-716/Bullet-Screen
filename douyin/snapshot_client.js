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
    next.roomId = snapshot?.room_id ?? previous.roomId ?? ""; next.roomTitle = snapshot?.room_title ?? previous.roomTitle ?? "待连接直播间"; next.sessionId = snapshot?.session_id ?? null; next.runId = snapshot?.run_id ?? null; next.generation = snapshot?.generation ?? 0; next.status = snapshot?.status ?? "idle"; next.connected = snapshot?.status === "connected"; next.workerAlive = !!snapshot?.worker_alive; next.dataSource = snapshot?.data_source ?? "none"; next.asOf = snapshot?.as_of ?? null; next.lastValidAt = snapshot?.last_valid_at ?? null; next.lastProtocolAt = snapshot?.last_protocol_at ?? null; next.protocolAvailable = snapshot?.protocol_available ?? previous.protocolAvailable ?? "unknown"; next.activityState = snapshot?.activity_state ?? snapshot?.freshness?.activity_state ?? previous.activityState ?? "quiet"; next.protocolHealth = snapshot?.protocol_health ?? snapshot?.freshness?.protocol_health ?? previous.protocolHealth ?? "unknown"; next.freshness = snapshot?.freshness ?? null; next.coverage = snapshot?.coverage ?? { coverage_state: "unknown" }; next.metrics = snapshot?.metrics ?? null; next.events = Array.isArray(snapshot?.events?.items) ? snapshot.events.items : []; next.lastError = snapshot?.error ?? ""; next.snapshotFailed = false; next.errorCode = null; next.diagnostics = snapshot?.diagnostics ?? {}; next.identityChanged = identityChanged; return next;
  }
  function resetViewState(previous) { return { ...previous, connected: false, sessionId: null, runId: null, generation: 0, status: "idle", metrics: null, events: [], lastValidAt: null, lastProtocolAt: null, protocolAvailable: "unknown", activityState: "unknown", protocolHealth: "unknown", freshness: null, coverage: { coverage_state: "unknown" }, lastError: "" }; }
  const statusText = (status, activityState, protocolAvailable) => status === "connected" && protocolAvailable === "false" ? "直播页面已打开，但暂时没有获取到互动数据" : status === "connected" && protocolAvailable === "unknown" ? "页面已打开 · 等待互动数据" : status === "connected" && activityState === "quiet" ? "采集正常 · 最近暂无新互动" : ({ connecting: "正在连接", authenticating: "正在连接", connected: "采集正常", stopping: "正在停止", stale: "采集可能中断", stopped: "已停止", error: "采集出错", offline: "服务离线", idle: "尚未开始采集" }[status] || "状态未知");
  const coverageText = (coverage) => ({ reliable_with_data: "数据连续可靠", reliable_no_events: "采集正常，这段时间没有新互动", gap: "这段时间存在采集缺口", unknown: "暂时无法判断数据是否完整" }[typeof coverage === "string" ? coverage : coverage?.coverage_state] || "暂时无法判断数据是否完整");
  const windowLabel = (value) => ({ "10s": "最近 10 秒", "60s": "最近 60 秒", "5m": "最近 5 分钟" }[value] || "最近 60 秒");
  function userFacingError(error, fallback) { const message = String(error || "").trim(), safeFallback = String(fallback || "操作没有成功，请检查状态后重试。"); if (/(?:Load failed|Failed to fetch|NetworkError|ERR_[A-Z_]+|HTTP\s*\d{3}|Traceback|TypeError|SyntaxError)/i.test(message)) return safeFallback; return /[\u3400-\u9fff]/.test(message) ? message : safeFallback; }
  const clockLabel = (value) => { const date = new Date(value); return Number.isNaN(date.getTime()) ? "—" : new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(date); };
  function dashboardPresentation(input = {}) {
    const coverage = typeof input.coverage === "string" ? input.coverage : input.coverage?.coverage_state;
    const status = input.serverAvailable === false ? "offline" : input.status || "idle";
    let headline = "状态未知", guidance = "请重新检查本地服务状态。", actions = ["retry"];
    if (input.errorCode === "snapshot_unstable") { headline = "状态正在变化，请重新检查"; guidance = "直播间或采集状态刚刚切换；重新读取后就能看到最新状态。"; }
    else if ((input.errorCode === "api_error" || input.snapshotFailed) && input.serverAvailable === false) { headline = "服务离线"; guidance = "本地服务没有响应。请启动 Bullet-Screen，或运行安装环境检查。"; actions = ["retry"]; }
    else if (input.errorCode === "api_error" || input.snapshotFailed) { headline = "本地服务暂时无法响应"; guidance = "本地服务没有响应。请点击“重新检查”；如果仍不行，请回到启动器重启服务或检查安装环境。"; }
    else if (input.errorCode === "room_invalid") { headline = "直播间信息有误"; guidance = "请修改直播间链接或房间号，再重新连接。"; actions = ["settings"]; }
    else if (input.errorCode === "connect_error") { headline = "启动失败"; guidance = "启动没有成功。请确认房间链接或房间号正确、房间正在直播，再试一次。"; actions = ["connect", "settings"]; }
    else if (input.mode === "demo" && status === "connecting") { headline = "正在准备演示数据"; guidance = "稍等片刻，演示事件和指标马上会出现。"; actions = []; }
    else if (input.mode === "demo" && status === "connected") { headline = "演示数据正在运行"; guidance = "页面中的互动和指标是演示内容，不是真实直播数据。"; actions = ["retry"]; }
    else if (status === "offline") { headline = "服务离线"; guidance = "本地服务没有响应。请启动 Bullet-Screen，或运行安装环境检查。"; actions = ["retry"]; }
    else if (status === "connecting" || status === "authenticating") { headline = "正在连接"; guidance = "正在打开直播间并建立采集连接，请稍等。"; actions = ["retry", "settings"]; }
    else if (status === "stopping") { headline = "正在停止"; guidance = "正在等待采集任务退出，完成前暂时不能重新启动。"; actions = []; }
    else if (status === "stopped" || status === "idle") { headline = status === "stopped" ? "已停止" : "尚未开始采集"; guidance = "选择直播间并点击连接即可开始。"; actions = ["connect", "settings"]; }
    else if (status === "error") { headline = "采集出错"; guidance = "采集没有成功。请检查房间是否正在直播、网络是否正常，再点击“重新连接”。"; actions = ["connect", "settings"]; }
    else if (status === "stale" || input.protocolHealth === "stale") { headline = "采集可能中断"; guidance = "最近没有收到可靠的直播数据。确认直播仍在进行，然后尝试重新连接。"; actions = ["connect", "settings"]; }
    else if (status === "connected") {
      if (input.protocolAvailable === "false" || input.protocolHealth === "unavailable") { headline = "直播页面已打开，但暂时没有获取到互动数据"; guidance = "确认直播仍在进行，检查登录状态，重新连接或尝试其他公开直播间；也可以先运行 Demo 检查本机安装。"; actions = ["connect", "settings", "login", "demo"]; }
      else if (input.protocolAvailable !== "true" || input.protocolHealth === "unknown") { headline = "页面已打开 · 等待互动数据"; guidance = "页面已经打开，仍在确认能否读取互动。若持续没有数据，可重新连接、检查登录状态或尝试其他公开直播间。"; actions = ["retry", "connect", "settings", "login", "demo"]; }
      else if (coverage === "gap") { headline = "存在采集缺口"; guidance = "所选时间内有一段时间没有可靠数据；这段时间的统计可能不完整。"; actions = ["retry", "connect"]; }
      else if (coverage === "unknown") { headline = "暂时无法判断数据是否完整"; guidance = "当前时间范围缺少足够的采集记录。重新检查状态，确认连接后再查看统计。"; actions = ["retry", "connect"]; }
      else if (input.activityState === "quiet" || coverage === "reliable_no_events") { headline = "采集正常 · 最近暂无新互动"; guidance = "采集连接正常，这个时间范围内暂时没有新的互动。"; actions = ["settings"]; }
      else { headline = "采集正常"; guidance = "正在持续接收直播互动数据。"; actions = ["settings"]; }
    }
    const room = input.roomTitle && input.roomTitle !== "待连接直播间" ? `${input.roomTitle}${input.roomId ? ` · ${input.roomId}` : ""}` : input.roomId ? `房间 ${input.roomId}` : "尚未选择直播间";
    const lastReliable = status === "stale" || status === "error" || status === "offline" || !!input.snapshotFailed;
    return {
      headline, guidance,
      coverageLabel: input.mode === "demo" ? "演示数据 · 不是真实直播" : coverageText(coverage),
      platformLabel: input.provider === "douyin" ? "Douyin" : "Bilibili",
      roomLabel: room, windowLabel: windowLabel(input.window),
      asOfLabel: input.asOf ? `数据截至：${clockLabel(input.asOf)}` : "数据截至：暂无可靠数据",
      snapshotLabel: input.asOf ? `${headline} · ${clockLabel(input.asOf)}` : headline,
      lastReliableLabel: lastReliable && input.lastValidAt ? `最后一次可靠数据：${clockLabel(input.lastValidAt)}` : "",
      actions,
      diagnostics: { status: input.status || "idle", coverage: coverage || "unknown", dataSource: input.dataSource || "none", protocolHealth: input.protocolHealth || "—", protocolAvailable: input.protocolAvailable || "—", error: input.error || "—", sessionId: input.sessionId || "—", runId: input.runId || "—", generation: input.generation ?? 0 },
    };
  }
  function clearSnapshotState(previous, error, errorCode) { const message = String(error || "snapshot failed"), code = errorCode || (/identity changed while creating snapshot|snapshot unstable/i.test(message) ? "snapshot_unstable" : "api_error"); return { ...previous, connected: false, lastError: message, snapshotFailed: true, errorCode: code }; }
  return { SnapshotClient, applySnapshotState, clearSnapshotState, resetViewState, statusText, coverageText, windowLabel, userFacingError, dashboardPresentation };
});
