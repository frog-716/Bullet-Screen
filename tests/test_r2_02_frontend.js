const assert = require("assert");
const fs = require("fs");
const path = require("path");

for (const provider of ["bilibili", "douyin"]) {
  const ui = require(path.join("..", provider, "signal_ui.js"));
  const html = ui.renderCard({
    signal_id: "sig-1",
    signal_type: "purchase_intent",
    strength: "strong",
    status: "active",
    rule_version: "rules-v2",
    coverage: "reliable_with_data",
    reason: "最近 60 秒内 2 个独立用户的 2 条消息命中购买意图规则。",
    event_count: 2,
    unique_user_count: 2,
    evidence: [{ event_id: "safe-event", summary: "想买" }],
    window_start: "2026-09-22T12:00:00.000+00:00",
    window_end: "2026-09-22T12:01:00.000+00:00",
  });
  assert(html.includes("rules-v2"));
  assert(html.includes("2 个独立用户"));
  assert(html.includes("data-feedback=\"useful\""));
  assert(html.includes("data-feedback=\"false_positive\""));
  assert(html.includes("data-feedback=\"note\""));
  assert(!html.includes("<script"));
  assert.strictEqual(ui.coverageText("gap"), "采集存在缺口");
  const source = fs.readFileSync(path.join(__dirname, "..", provider, "app.js"), "utf8");
  assert(source.includes("/api/signals/feedback"));
  assert(source.includes("BulletScreenSignals.renderCard"));
}

console.log("R2-02 frontend contract tests passed");
