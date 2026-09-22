(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.BulletScreenOnboarding = factory();
})(typeof globalThis === "object" ? globalThis : this, function () {
  function normalizeRoomInput(value) {
    const raw = String(value || "").trim();
    if (/^\d+$/.test(raw)) return raw;
    try {
      const url = new URL(raw);
      if (!/^https?:$/.test(url.protocol) || url.hostname.toLowerCase() !== "live.douyin.com") return "";
      const room = url.pathname.split("/").filter(Boolean)[0] || "";
      return /^\d+$/.test(room) ? raw : "";
    } catch (_) {
      return "";
    }
  }

  function readLaunchOptions(href) {
    const params = new URL(href || "http://127.0.0.1/").searchParams;
    return {
      roomInput: normalizeRoomInput(params.get("room_id") || params.get("room_url") || ""),
      demo: params.get("demo") === "1",
      autostart: params.get("autostart") === "1",
    };
  }

  return { normalizeRoomInput, readLaunchOptions };
});
