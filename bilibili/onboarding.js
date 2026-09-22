(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.BulletScreenOnboarding = factory();
})(typeof globalThis === "object" ? globalThis : this, function () {
  function normalizeRoomInput(value) {
    const raw = String(value || "").trim();
    if (/^\d+$/.test(raw)) return raw;
    try {
      const url = new URL(raw);
      if (!/^https?:$/.test(url.protocol)) return "";
      if (!new Set(["live.bilibili.com", "www.live.bilibili.com"]).has(url.hostname.toLowerCase())) return "";
      const room = url.pathname.split("/").filter(Boolean)[0] || "";
      return /^\d+$/.test(room) ? room : "";
    } catch (_) {
      return "";
    }
  }

  function readLaunchOptions(href) {
    const params = new URL(href || "http://127.0.0.1/").searchParams;
    const roomInput = normalizeRoomInput(params.get("room_id") || params.get("room_url") || "");
    return { roomInput, demo: params.get("demo") === "1", autostart: params.get("autostart") === "1" };
  }

  return { normalizeRoomInput, readLaunchOptions };
});
