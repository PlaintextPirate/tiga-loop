// Zoom75 TIGA screen media format (DLX container) and BLE chunking.
// Port of src/tiga_tool/protocol.py; tests in web/test/dlx.test.mjs check it
// byte-for-byte against a captured working Pocket Wuque upload.

export const WIDTH = 320;
export const HEIGHT = 172;
export const FRAME_BYTES = WIDTH * HEIGHT * 2;
export const FILE_START = 25;          // 8-byte envelope + 17-byte preamble
export const MAX_FRAMES = 50;

export const CONTROL1 = Uint8Array.from([0x88, 0, 0, 0, 0, 0, 0x10, 0, 0x09, 0x03, 0x04, 0, 0x08, 0, 0,
  0xff, 0xfc, 0, 0x03, 0x5d, 0xd0, 0x05, 0, 0]);
export const CONTROL2 = Uint8Array.from([0x88, 0, 0, 0, 0, 0, 0x0a, 0, 0x08, 0x03, 0, 0xff, 0xff, 0xff, 0,
  0xff, 0xff, 0xff]);

const hex = (s) => Uint8Array.from(s.trim().split(/\s+/), (h) => parseInt(h, 16));
export const C1_ACKS = [hex("88 00 00 00 00 00 05 0e 07 02 01 09 03"), hex("88 00 00 00 00 00 03 0f 09 04 02")];
export const C2_ACKS = [hex("88 00 00 00 00 00 05 0f 07 02 01 08 03"), hex("88 00 00 00 00 00 03 0e 08 04 02")];
export const PROGRESS = hex("88 00 00 00 00 00 03 0b 08 06 05");
export const COMPLETE = hex("88 00 00 00 00 00 03 0c 08 06 02");
const PREAMBLE = hex("08 05 01 00 09 00 ff ff ff 00 ff ff ff 02 02 ff ff");

export function sameBytes(a, b) {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

/** RGBA pixels (320x172, e.g. from canvas getImageData) -> RGB565 big-endian frame. */
export function rgb565be(rgba) {
  const out = new Uint8Array(FRAME_BYTES);
  for (let i = 0, o = 0; o < FRAME_BYTES; i += 4, o += 2) {
    const v = ((rgba[i] & 0xf8) << 8) | ((rgba[i + 1] & 0xfc) << 3) | (rgba[i + 2] >> 3);
    out[o] = v >> 8;
    out[o + 1] = v & 0xff;
  }
  return out;
}

/** What the screen will show: RGB565 back to RGBA (for previews). */
export function rgb565ToRgba(frame, rgba = new Uint8ClampedArray(WIDTH * HEIGHT * 4)) {
  for (let i = 0, o = 0; i < frame.length; i += 2, o += 4) {
    const v = (frame[i] << 8) | frame[i + 1];
    const r = (v >> 11) & 31, g = (v >> 5) & 63, b = v & 31;
    rgba[o] = (r << 3) | (r >> 2);
    rgba[o + 1] = (g << 2) | (g >> 4);
    rgba[o + 2] = (b << 3) | (b >> 2);
    rgba[o + 3] = 255;
  }
  return rgba;
}

/**
 * Multi-frame DLX upload stream, laid out as Pocket Wuque builds it:
 *   header (\0DLX fc ff 00 00, 0x0160, 3 records, frame count, 00 01) + 324 x ff
 *   section table (u32 LE): records @368, frame index @452, pixels, end
 *   3 x 28-byte records; record 2 = animation: delay byte (11 - speed), frame count
 *   frame index (u32 LE per frame), RGB565 BE frames, trailer fc ff 00 00
 * A single picture is sent as two identical frames at speed 1 (the captured still layout).
 */
export function buildStream(frames, speed = 5) {
  if (frames.length === 1) { frames = [frames[0], frames[0]]; speed = 1; }
  const n = frames.length;
  if (n < 1 || n > MAX_FRAMES) throw new Error(`1-${MAX_FRAMES} frames`);
  if (!(speed >= 1 && speed <= 10)) throw new Error("speed must be 1-10");
  for (const f of frames) if (f.length !== FRAME_BYTES) throw new Error("bad frame size");

  const recordsLen = 3 * 28, indexLen = 4 * n;
  const pixAt = 368 + recordsLen + indexLen;
  const dlxLen = pixAt + n * FRAME_BYTES + 4;
  const bodyLen = PREAMBLE.length + dlxLen;
  const s = new Uint8Array(8 + bodyLen);
  const dv = new DataView(s.buffer);

  // envelope: 88 00 00 00 <u24 BE body length> 00
  s.set([0x88, 0, 0, 0, (bodyLen >> 16) & 0xff, (bodyLen >> 8) & 0xff, bodyLen & 0xff, 0], 0);
  s.set(PREAMBLE, 8);

  const f = FILE_START;
  s.set([0x00, 0x44, 0x4c, 0x58, 0xfc, 0xff, 0x00, 0x00], f);
  dv.setUint16(f + 8, 352, true);
  dv.setUint16(f + 10, 3, true);
  dv.setUint16(f + 12, n, true);
  s.set([0x00, 0x01], f + 14);
  s.fill(0xff, f + 28, f + 352);
  dv.setUint32(f + 352, 368, true);
  dv.setUint32(f + 356, 368 + recordsLen, true);
  dv.setUint32(f + 360, pixAt, true);
  dv.setUint32(f + 364, pixAt + n * FRAME_BYTES, true);

  const wh = [0, 0, 0x01, 0x40, 0x00, 0xac];                     // 320 x 172
  const rec = (head, at) => { s.set(head, f + at); s.set(wh, f + at + 8); };
  rec([0x15, 1, 0, 0, 0, 0, 0, 1], 368);
  rec([0x14, 3, 0, 1, 11 - speed, 0, 0, n], 396);
  rec([0x14, 4, 0, 0, 0, 0, 0, 1], 424);

  for (let k = 0; k < n; k++) dv.setUint32(f + 452 + 4 * k, k * FRAME_BYTES, true);
  for (let k = 0; k < n; k++) s.set(frames[k], f + pixAt + k * FRAME_BYTES);
  s.set([0xfc, 0xff, 0x00, 0x00], f + pixAt + n * FRAME_BYTES);
  validateStream(s);
  return s;
}

export function validateStream(s) {
  const len = (s[4] << 16) | (s[5] << 8) | s[6];
  if (s[0] !== 0x88 || s[1] || s[2] || s[3] || s[7] || len !== s.length - 8) throw new Error("envelope mismatch");
  if (!sameBytes(s.subarray(8, 25), PREAMBLE)) throw new Error("preamble mismatch");
  if (!sameBytes(s.subarray(25, 33), Uint8Array.from([0, 0x44, 0x4c, 0x58, 0xfc, 0xff, 0, 0]))) throw new Error("DLX prefix mismatch");
  if (!sameBytes(s.subarray(s.length - 4), Uint8Array.from([0xfc, 0xff, 0, 0]))) throw new Error("trailer mismatch");
}

/** CONTROL1 announces the DLX file length (u32 BE, bytes 17-20). */
export function control1For(stream) {
  const c = CONTROL1.slice();
  new DataView(c.buffer).setUint32(17, stream.length - FILE_START, false);
  return c;
}

/** 181 data bytes + XOR per BLE write, never crossing a 4096-byte boundary of the DLX file. */
export function chunkStream(s) {
  validateStream(s);
  const out = [];
  let off = 0;
  while (off < s.length) {
    let n = Math.min(181, s.length - off);
    if (off >= FILE_START) n = Math.min(n, 4096 - ((off - FILE_START) % 4096));
    const v = new Uint8Array(n + 1);
    v.set(s.subarray(off, off + n));
    let x = 0;
    for (let i = 0; i < n; i++) x ^= v[i];
    v[n] = x;
    off += n;
    out.push({ value: v, endsBlock: off >= FILE_START && (off - FILE_START) % 4096 === 0 });
  }
  return out;
}

/** Evenly spaced subset of indices, as the app does for long GIFs. */
export function pickEven(indices, limit) {
  if (indices.length <= limit) return indices.slice();
  const step = indices.length / limit;
  return Array.from({ length: limit }, (_, k) => indices[Math.floor(k * step)]);
}

export const BYTES_PER_SECOND = 13_900;   // measured on a TIGA from macOS
