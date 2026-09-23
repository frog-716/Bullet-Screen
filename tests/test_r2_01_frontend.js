const assert = require("assert");

const bili = require("../bilibili/snapshot_client.js");
const douyin = require("../douyin/snapshot_client.js");

async function testClientIgnoresLateResponse(api) {
  const deferred = [];
  const applied = [];
  const client = new api.SnapshotClient(({ requestGeneration }) => new Promise((resolve) => {
    deferred.push({ requestGeneration, resolve });
  }), (snapshot) => applied.push(snapshot));

  const first = client.refresh();
  const second = client.refresh();
  deferred[1].resolve({ generation: 2, session_id: "new" });
  await second;
  deferred[0].resolve({ generation: 1, session_id: "old" });
  await first;
  assert.deepStrictEqual(applied, [{ generation: 2, session_id: "new" }]);
}

function testStateHelpers(api) {
  const initial = {
    roomId: "room-a", sessionId: "session-a", runId: "run-a", generation: 1,
    events: [{ event_id: "old" }], metrics: { total: 1 }, status: "connected",
  };
  const snapshot = api.applySnapshotState(initial, {
    room_id: "room-b", session_id: "session-b", run_id: "run-b", generation: 2,
    status: "connected", events: { items: [{ event_id: "new" }] }, metrics: { total: 2 },
  });
  assert.strictEqual(snapshot.identityChanged, true);
  assert.deepStrictEqual(snapshot.events, [{ event_id: "new" }]);
  assert.strictEqual(snapshot.sessionId, "session-b");
  const zero = api.applySnapshotState(snapshot, {
    room_id: "room-b", session_id: "session-b", run_id: "run-b", generation: 2,
    status: "connected", events: { items: [] }, metrics: { online: 0 },
  });
  assert.strictEqual(zero.metrics.online, 0);
  if (api === douyin) {
    const quiet = api.applySnapshotState(snapshot, {
      room_id: "room-b", session_id: "session-b", run_id: "run-b", generation: 2,
      status: "connected", activity_state: "quiet", protocol_health: "healthy",
      protocol_available: "true",
      last_protocol_at: "2026-09-22T00:00:01Z", last_valid_at: null,
      events: { items: [] }, metrics: { online: null },
    });
    assert.strictEqual(quiet.activityState, "quiet");
    assert.strictEqual(quiet.protocolHealth, "healthy");
    assert.strictEqual(quiet.protocolAvailable, "true");
    assert.strictEqual(quiet.lastProtocolAt, "2026-09-22T00:00:01Z");
    assert.strictEqual(api.statusText("connected", "unknown", "unknown"), "页面已打开 · 等待互动数据");
    assert.strictEqual(api.statusText("connected", "unknown", "false"), "直播页面已打开，但暂时没有获取到互动数据");
  }
  assert.strictEqual(api.statusText("stopping"), "正在停止");
  assert.strictEqual(api.statusText("stale"), "采集可能中断");
  if (api === douyin) assert.strictEqual(api.statusText("connected", "quiet", "true"), "采集正常 · 最近暂无新互动");
  assert.strictEqual(api.coverageText({ coverage_state: "unknown" }), "暂时无法判断数据是否完整");
  assert.strictEqual(api.coverageText({ coverage_state: "gap" }), "这段时间存在采集缺口");
  const cleared = api.clearSnapshotState(snapshot, "snapshot failed");
  assert.deepStrictEqual(cleared.events, [{ event_id: "new" }]);
  assert.strictEqual(cleared.metrics.total, 2);
  assert.strictEqual(cleared.lastError, "snapshot failed");
  const reset = api.resetViewState(snapshot);
  assert.strictEqual(reset.roomId, "room-b");
  assert.deepStrictEqual(reset.events, []);
}

(async () => {
  for (const api of [bili, douyin]) {
    await testClientIgnoresLateResponse(api);
    testStateHelpers(api);
  }
  console.log("r2-01 frontend contract: PASS");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
