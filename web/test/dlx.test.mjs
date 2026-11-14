import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { test } from "node:test";
import assert from "node:assert/strict";
import { buildStream, chunkStream, control1For, CONTROL1, FRAME_BYTES, FILE_START, rgb565be, rgb565ToRgba, pickEven } from "../dlx.js";

const tpl = new Uint8Array(readFileSync(new URL("../../src/tiga_tool/template/known_good_2frame.bin", import.meta.url)));
const F1 = FILE_START + 8 + 452, F2 = F1 + FRAME_BYTES;

test("rebuilds the captured Pocket Wuque upload byte-for-byte", () => {
  const s = buildStream([tpl.slice(F1, F1 + FRAME_BYTES), tpl.slice(F2, F2 + FRAME_BYTES)], 1);
  assert.deepEqual(s, tpl);
  assert.deepEqual(control1For(s), CONTROL1);
});

test("chunking matches the capture: 1239 writes, 53 blocks", () => {
  const c = chunkStream(tpl);
  assert.equal(c.length, 1239);
  assert.equal(c.filter((x) => x.endsBlock).length, 53);
  assert.ok(c.every((x) => x.value.length <= 182));
});

test("matches the Python builder for 9 frames (bytes from tiga_tool.protocol)", () => {
  const frames = Array.from({ length: 9 }, (_, k) => new Uint8Array(FRAME_BYTES).fill(k));
  const s = buildStream(frames, 5);
  // sha256 of tiga_tool.protocol.build_animation(same frames, speed=5)
  assert.equal(createHash("sha256").update(s).digest("hex"), "0ab18e264beedbd8acd72d751aec547d7fc38d999c189e9f509de4c62f6479fa");
});

test("single picture becomes the two-frame still layout", () => {
  const f = new Uint8Array(FRAME_BYTES).fill(7);
  const s = buildStream([f], 5);
  assert.equal(s.length, tpl.length);
  assert.deepEqual(s.subarray(0, F1), tpl.subarray(0, F1));
});

test("refuses more than the confirmed 50-frame limit", () => {
  const f = new Uint8Array(FRAME_BYTES);
  assert.equal(buildStream(Array(50).fill(f), 5).length, 8 + 17 + 456 + 50 * (FRAME_BYTES + 4));
  assert.throws(() => buildStream(Array(51).fill(f), 5), /1-50 frames/);
});

test("colour conversion round-trips pure colours", () => {
  const rgba = new Uint8ClampedArray(320 * 172 * 4);
  for (let i = 0; i < rgba.length; i += 4) rgba.set([255, 0, 0, 255], i);
  const f = rgb565be(rgba);
  assert.deepEqual([f[0], f[1]], [0xf8, 0x00]);
  assert.deepEqual([...rgb565ToRgba(f).subarray(0, 4)], [255, 0, 0, 255]);
});

test("pickEven thins evenly and keeps short lists whole", () => {
  assert.deepEqual(pickEven([0, 1, 2], 9), [0, 1, 2]);
  assert.deepEqual(pickEven([...Array(100).keys()], 4), [0, 25, 50, 75]);
});
