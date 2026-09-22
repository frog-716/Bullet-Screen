(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.BulletScreenSignals = factory();
})(typeof self !== "undefined" ? self : this, function () {
  const escapeHtml = (value) => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
  const labels = { purchase_intent: "购买意图", recommendation: "正向推荐" };
  const coverage = { reliable_with_data: "采集可靠，有匹配数据", reliable_no_events: "采集可靠，窗口内没有事件", gap: "采集存在缺口", unknown: "证据不足，无法判断" };
  const strength = { strong: "强", moderate: "中", weak: "弱" };
  const statuses = { active: "生效", cleared: "已清除", expired: "已过期" };
  function renderCard(signal) {
    const evidenceCount = Array.isArray(signal.evidence) ? signal.evidence.length : Number(signal.event_count || 0);
    const cls = signal.status === "cleared" ? "cleared" : signal.strength || "weak";
    return `<div class="signal-row ${escapeHtml(cls)}" data-signal-id="${escapeHtml(signal.signal_id)}"><div class="signal-title"><span class="signal-dot"></span><strong>${escapeHtml(labels[signal.signal_type] || signal.signal_type || "规则信号")}</strong><em>${escapeHtml(strength[signal.strength] || signal.strength || "弱")} · ${escapeHtml(statuses[signal.status] || signal.status || "生效")}</em></div><p>${escapeHtml(signal.reason || "暂无事实说明")}</p><small>窗口 ${escapeHtml(signal.window_start || "—")} → ${escapeHtml(signal.window_end || "—")} · ${escapeHtml(coverage[signal.coverage] || signal.coverage || "证据不足")}</small><small>命中 ${escapeHtml(signal.event_count ?? 0)} 条 · 独立用户 ${escapeHtml(signal.unique_user_count ?? 0)} · 证据 ${escapeHtml(evidenceCount)} 条 · ${escapeHtml(signal.rule_version || "—")}</small><div class="signal-feedback"><button type="button" data-feedback="useful">有用</button><button type="button" data-feedback="false_positive">误报</button><input type="text" maxlength="240" placeholder="备注（可选）" data-feedback-note /><button type="button" data-feedback="note">保存备注</button></div></div>`;
  }
  return { renderCard, coverageText: (value) => coverage[value] || value || "证据不足，无法判断" };
});
