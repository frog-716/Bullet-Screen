const assert = require("assert");

const bili = require("../bilibili/onboarding.js");
const douyin = require("../douyin/onboarding.js");

assert.strictEqual(bili.normalizeRoomInput("12345"), "12345");
assert.strictEqual(bili.normalizeRoomInput("https://live.bilibili.com/12345?foo=bar"), "12345");
assert.strictEqual(bili.normalizeRoomInput("https://example.com/12345"), "");
assert.strictEqual(douyin.normalizeRoomInput("511304254586"), "511304254586");
assert.strictEqual(douyin.normalizeRoomInput("https://live.douyin.com/511304254586"), "https://live.douyin.com/511304254586");
assert.strictEqual(douyin.normalizeRoomInput(""), "");

const biliLaunch = bili.readLaunchOptions("https://127.0.0.1:4173/?room_id=12345");
assert.deepStrictEqual(biliLaunch, { roomInput: "12345", demo: false, autostart: false });
const demoLaunch = douyin.readLaunchOptions("https://127.0.0.1:4173/?demo=1&autostart=1");
assert.deepStrictEqual(demoLaunch, { roomInput: "", demo: true, autostart: true });

console.log("first-run frontend contract: PASS");
