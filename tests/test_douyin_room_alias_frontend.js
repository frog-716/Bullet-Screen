const assert = require("assert");
const onboarding = require("../douyin/onboarding.js");

const shareUrl = "https://live.douyin.com/keyis153";
assert.strictEqual(onboarding.normalizeRoomInput(shareUrl), shareUrl);
assert.strictEqual(
  onboarding.readLaunchOptions(`http://127.0.0.1:4173/?room_url=${encodeURIComponent(shareUrl)}`).roomInput,
  shareUrl,
);
assert.strictEqual(onboarding.normalizeRoomInput("https://example.com/keyis153"), "");
assert.strictEqual(onboarding.normalizeRoomInput("keyis153"), "");
console.log("Douyin share room input contract: PASS");
