const assert = require("assert");
const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const coverageLabels = {
  reliable_with_data: "数据连续可靠",
  reliable_no_events: "采集正常，这段时间没有新互动",
  gap: "这段时间存在采集缺口",
  unknown: "暂时无法判断数据是否完整",
};
const windowLabels = { "10s": "最近 10 秒", "60s": "最近 60 秒", "5m": "最近 5 分钟" };

for (const provider of ["bilibili", "douyin"]) {
  const snapshot = require(path.join(ROOT, provider, "snapshot_client.js"));
  const signalUI = require(path.join(ROOT, provider, "signal_ui.js"));
  assert.strictEqual(typeof snapshot.dashboardPresentation, "function", `${provider} exports dashboardPresentation`);
  assert.strictEqual(typeof snapshot.windowLabel, "function", `${provider} exports windowLabel`);

  for (const [coverage, expected] of Object.entries(coverageLabels)) {
    assert.strictEqual(snapshot.coverageText({ coverage_state: coverage }), expected, `${provider} translates ${coverage}`);
  }
  for (const [window, expected] of Object.entries(windowLabels)) {
    assert.strictEqual(snapshot.windowLabel(window), expected, `${provider} labels ${window}`);
  }
  const cached = { metrics: { total: 3 }, events: [{ id: "e1" }], lastValidAt: "2026-09-23T12:34:50Z" };
  const failedState = snapshot.clearSnapshotState(cached, "service unavailable");
  assert.strictEqual(failedState.connected, false);
  assert.strictEqual(failedState.metrics, cached.metrics);
  assert.strictEqual(failedState.events, cached.events);
  assert.strictEqual(failedState.lastValidAt, cached.lastValidAt);
  assert.strictEqual(failedState.errorCode, "api_error");
  assert.strictEqual(snapshot.clearSnapshotState(cached, "collector identity changed while creating snapshot").errorCode, "snapshot_unstable");
  assert.strictEqual(snapshot.userFacingError("Load failed", "请重新检查本地服务。"), "请重新检查本地服务。");
  assert.strictEqual(snapshot.userFacingError("本地服务异常：Load failed", "请重新检查本地服务。"), "请重新检查本地服务。");
  assert.strictEqual(snapshot.userFacingError("网络请求失败", "请重新检查本地服务。"), "网络请求失败");

  const present = (overrides = {}) => snapshot.dashboardPresentation({
    provider,
    serverAvailable: true,
    status: "connected",
    protocolAvailable: provider === "douyin" ? "true" : undefined,
    protocolHealth: provider === "douyin" ? "healthy" : undefined,
    activityState: "active",
    coverage: { coverage_state: "reliable_with_data" },
    roomTitle: "公开直播间",
    roomId: "123456",
    window: "60s",
    asOf: "2026-09-23T12:34:56Z",
    lastValidAt: "2026-09-23T12:34:50Z",
    ...overrides,
  });

  const reliable = present();
  assert.strictEqual(reliable.headline, "采集正常");
  assert.strictEqual(reliable.roomLabel, "公开直播间 · 123456");
  assert.strictEqual(reliable.windowLabel, "最近 60 秒");
  assert.match(reliable.asOfLabel, /^数据截至：/);
  assert(!/generation|run_id|session_id/i.test(reliable.headline));

  const quiet = present({ activityState: "quiet", coverage: { coverage_state: "reliable_no_events" } });
  assert.match(quiet.headline, /最近暂无新互动/);
  assert.strictEqual(quiet.coverageLabel, coverageLabels.reliable_no_events);

  assert.strictEqual(present({ coverage: { coverage_state: "gap" } }).headline, "存在采集缺口");
  assert.strictEqual(present({ coverage: { coverage_state: "unknown" } }).headline, "暂时无法判断数据是否完整");

  const stale = present({ status: "stale", protocolHealth: "stale" });
  assert.strictEqual(stale.headline, "采集可能中断");
  assert.match(stale.lastReliableLabel, /^最后一次可靠数据：\d{2}:\d{2}:\d{2}$/);
  assert(stale.actions.includes("connect"));

  if (provider === "douyin") {
    assert.strictEqual(present({ protocolAvailable: "unknown", protocolHealth: "unknown" }).headline, "页面已打开 · 等待互动数据");
    const unavailable = present({ protocolAvailable: "false", protocolHealth: "unavailable" });
    assert.strictEqual(unavailable.headline, "直播页面已打开，但暂时没有获取到互动数据");
    assert(unavailable.actions.includes("demo"));
    assert(unavailable.actions.includes("settings"));
    assert(unavailable.actions.includes("login"));
  }

  assert.strictEqual(present({ status: "connecting" }).headline, "正在连接");
  assert.strictEqual(present({ status: "stopping" }).headline, "正在停止");
  const stoppedSnapshot = snapshot.applySnapshotState({ roomId: "123456", serverAvailable: true }, {
    status: "stopped", worker_alive: false, room_id: "123456", events: { items: [] },
    coverage: { coverage_state: "reliable_no_events" },
  });
  const stopped = present({ ...stoppedSnapshot, serverAvailable: true });
  assert.strictEqual(stopped.headline, "采集已停止");
  assert(!stopped.guidance.includes("本地服务没有响应"));
  assert(stopped.actions.includes("connect"));
  assert.strictEqual(snapshot.statusText("stopped"), "采集已停止");
  assert.strictEqual(snapshot.statusText("service_offline"), "本地服务没有响应");
  assert.strictEqual(present({ status: "offline", serverAvailable: true, workerAlive: false }).headline, "采集已停止");
  const serviceOffline = present({ status: "stopped", serverAvailable: false, snapshotFailed: true, errorCode: "api_error" });
  assert.strictEqual(serviceOffline.headline, "本地服务没有响应");
  assert(serviceOffline.guidance.includes("检查安装环境"));
  assert(serviceOffline.actions.includes("retry"));
  assert.strictEqual(present({ status: "connected", serverAvailable: true }).headline, "采集正常");
  assert.strictEqual(present({ status: "stopping", serverAvailable: true }).headline, "正在停止");
  assert.strictEqual(present({ status: "stale", serverAvailable: true }).headline, "采集可能中断");
  assert.strictEqual(present({ status: "error", error: "采集异常" }).headline, "采集出错");
  assert.strictEqual(present({ errorCode: "connect_error", error: "房间未开播" }).headline, "启动失败");
  assert.strictEqual(present({ status: "idle", workerAlive: true }).headline, "尚未开始采集");

  const unstable = present({ status: "error", errorCode: "snapshot_unstable", error: "状态刚刚变化" });
  assert.strictEqual(unstable.headline, "状态正在变化，请重新检查");
  assert(unstable.actions.includes("retry"));

  const apiError = present({ status: "error", errorCode: "api_error", error: "本地服务没有响应" });
  assert.strictEqual(apiError.guidance, "本地服务没有响应。请点击“重新检查”；如果仍不行，请回到启动器重启服务或检查安装环境。");
  assert(apiError.actions.includes("retry"));
  const lowLevelApiError = present({ status: "error", errorCode: "api_error", error: "Load failed" });
  assert(!lowLevelApiError.guidance.includes("Load failed"));
  assert.strictEqual(lowLevelApiError.diagnostics.error, "Load failed");
  assert.match(present({ snapshotFailed: true, error: "本地服务没有响应", lastValidAt: "2026-09-23T12:34:50Z" }).lastReliableLabel, /^最后一次可靠数据：/);

  const demo = present({ mode: "demo", coverage: { coverage_state: "gap" } });
  assert.strictEqual(demo.headline, "演示数据正在运行");
  assert.strictEqual(demo.coverageLabel, "演示数据 · 不是真实直播");
  assert(!demo.snapshotLabel.includes("采集缺口"), `${provider} demo summary must not expose lifecycle gaps`);
  assert.strictEqual(present({ mode: "demo", status: "connecting", coverage: { coverage_state: "gap" } }).coverageLabel, "演示数据 · 不是真实直播");

  const diagnostics = present({ sessionId: "private-session", runId: "run-123", generation: 8 }).diagnostics;
  assert.strictEqual(diagnostics.sessionId, "private-session");
  assert.strictEqual(diagnostics.runId, "run-123");
  assert.strictEqual(diagnostics.generation, 8);

  const signal = signalUI.renderCard({
    signal_id: "sig-1", signal_type: "purchase_intent", strength: "strong", status: "active",
    evidence: [{ event_id: "internal-id-must-not-display", event_type: "comment", event_time: "2026-09-23T12:34:50Z", summary: "想买这个" }],
  });
  assert(signal.includes("查看证据"));
  assert.match(signal, /\d{2}:\d{2}:\d{2}/);
  assert(signal.includes("评论"));
  assert(signal.includes("想买这个"));
  assert(!signal.includes("internal-id-must-not-display"));
  assert.strictEqual(signalUI.coverageText("gap"), coverageLabels.gap);
  assert.strictEqual(signalUI.emptyText({ started: false }), "尚未开始采集");
  assert.match(signalUI.emptyText({ started: true, coverageState: "gap" }), /存在采集缺口/);
  assert.match(signalUI.emptyText({ started: true, coverageState: "unknown" }), /暂时无法判断/);
  assert.match(signalUI.emptyText({ started: true, coverageState: "reliable_no_events" }), /没有新互动，也没有触发信号/);
  if (provider === "douyin") assert.match(signalUI.emptyText({ started: true, coverageState: "gap", demo: true }), /演示数据/);

  const html = fs.readFileSync(path.join(ROOT, provider, "index.html"), "utf8");
  const app = fs.readFileSync(path.join(ROOT, provider, "app.js"), "utf8");
  assert(html.includes('id="truthPanel"'));
  assert(html.includes('id="diagnosticDetails"'));
  assert(html.includes('id="diagnosticError"'));
  if (provider === "douyin") {
    assert(html.includes('id="storageModeLabel"'));
    assert(app.includes("演示内容只在运行期间保留，不写入本机文件"));
  }
  assert(app.includes("dashboardPresentation"));
  assert(app.includes('"service_offline"'));
  assert(app.includes("data-truth-action"));
  assert(app.includes('diagnosticError").textContent=model.diagnostics.error'));
  assert.match(app, /function showToast\(message\).*userFacingError/s, `${provider} toast must hide raw browser/network errors`);
  assert.match(app, /snapshotLabel"\)\.textContent\s*=\s*model\.snapshotLabel/);
}

const douyinApp = fs.readFileSync(path.join(ROOT, "douyin", "app.js"), "utf8");
const douyinHtml = fs.readFileSync(path.join(ROOT, "douyin", "index.html"), "utf8");
assert(douyinHtml.includes("固定范围"));
assert(douyinHtml.includes("数值使用上方所选范围"));
assert(!douyinApp.includes('textContent="60 秒"'));

console.log("Dashboard truthfulness contract tests passed");
