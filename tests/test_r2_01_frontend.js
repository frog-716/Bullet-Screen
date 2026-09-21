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
  assert.strictEqual(api.statusText("stopping"), "正在停止，暂时不能重新启动");
  assert.strictEqual(api.statusText("stale"), "服务还活着，但最近没有可靠数据");
  assert.strictEqual(api.coverageText({ coverage_state: "unknown" }), "数据完整性未知");
  assert.strictEqual(api.coverageText({ coverage_state: "gap" }), "存在采集缺口");
  const cleared = api.clearSnapshotState(snapshot, "snapshot failed");
  assert.deepStrictEqual(cleared.events, []);
  assert.strictEqual(cleared.metrics, null);
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
