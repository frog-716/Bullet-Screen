(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.BulletScreenSignals = factory();
})(typeof self !== "undefined" ? self : this, function () {
  const escapeHtml = (value) => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
  const labels = { purchase_intent: "购买意图", recommendation: "正向推荐" };
  const coverage = { reliable_with_data: "数据连续可靠", reliable_no_events: "采集正常，这段时间没有新互动", gap: "这段时间存在采集缺口", unknown: "暂时无法判断数据是否完整" };
  const strength = { strong: "强", moderate: "中", weak: "弱" };
  const statuses = { active: "生效", cleared: "已清除", expired: "已过期" };
  const eventTypes = { comment: "评论", danmaku: "弹幕", gift: "礼物", like: "点赞", follow: "关注", share: "分享", entry: "进场", viewer_change: "在线人数" };
  function formatEvidenceTime(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "时间未知" : new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(date);
  }
  function renderCard(signal) {
    const evidenceCount = Array.isArray(signal.evidence) ? signal.evidence.length : Number(signal.event_count || 0);
    const cls = signal.status === "cleared" ? "cleared" : signal.strength || "weak";
    const evidence = Array.isArray(signal.evidence) ? signal.evidence : [];
    const evidenceHtml = evidence.length ? `<details class="signal-evidence"><summary>查看证据（${evidence.length} 条）</summary><ul>${evidence.slice(0, 20).map((item) => `<li><time>${escapeHtml(formatEvidenceTime(item.event_time))}</time><b>${escapeHtml(eventTypes[item.event_type] || item.event_type || "互动")}</b><span>${escapeHtml(item.summary || "无文字摘要")}</span></li>`).join("")}</ul></details>` : `<small class="signal-evidence-empty">暂时没有可展示的事件证据</small>`;
    return `<div class="signal-row ${escapeHtml(cls)}" data-signal-id="${escapeHtml(signal.signal_id)}"><div class="signal-title"><span class="signal-dot"></span><strong>${escapeHtml(labels[signal.signal_type] || signal.signal_type || "规则信号")}</strong><em>${escapeHtml(strength[signal.strength] || signal.strength || "弱")} · ${escapeHtml(statuses[signal.status] || signal.status || "生效")}</em></div><p>${escapeHtml(signal.reason || "暂无事实说明")}</p><small>窗口 ${escapeHtml(signal.window_start || "—")} → ${escapeHtml(signal.window_end || "—")} · ${escapeHtml(coverage[signal.coverage] || signal.coverage || "证据不足")}</small><small>命中 ${escapeHtml(signal.event_count ?? 0)} 条 · 独立用户 ${escapeHtml(signal.unique_user_count ?? 0)} · 证据 ${escapeHtml(evidenceCount)} 条 · ${escapeHtml(signal.rule_version || "—")}</small>${evidenceHtml}<div class="signal-feedback"><button type="button" data-feedback="useful">有用</button><button type="button" data-feedback="false_positive">误报</button><input type="text" maxlength="240" placeholder="备注（可选）" data-feedback-note /><button type="button" data-feedback="note">保存备注</button></div></div>`;
  }
  function emptyText({ started = false, coverageState = "unknown", demo = false } = {}) {
    if (!started) return "尚未开始采集";
    if (demo) return "演示数据中暂时没有规则信号。";
    if (coverageState === "gap") return "这段时间存在采集缺口，暂时无法判断是否出现信号。";
    if (coverageState === "unknown") return "暂时无法判断这段时间是否出现信号。";
    if (coverageState === "reliable_no_events") return "采集正常，这段时间没有新互动，也没有触发信号。";
    return "当前统计范围内没有触发可确认的信号。";
  }
  return { renderCard, emptyText, coverageText: (value) => coverage[value] || value || "证据不足，无法判断" };
});
