// Web Bluetooth upload to the Zoom75 TIGA screen. Same sequence as
// src/tiga_tool/image.py: CONTROL1 -> 2 ACKs, CONTROL2 -> 2 ACKs, bulk writes with a
// PROGRESS notification after every 4 KB block, then COMPLETE.

import { C1_ACKS, C2_ACKS, COMPLETE, CONTROL2, PROGRESS, chunkStream, control1For, sameBytes } from "./dlx.js";

export const SERVICE = "1f40eaf8-aab4-14a3-f1ba-f61f35cddbaa";
const CHAR = (n) => `1f40000${n}-aab4-14a3-f1ba-f61f35cddbaa`;

export class UploadError extends Error {
  constructor(message, { afterControl2 = false } = {}) {
    super(message);
    this.afterControl2 = afterControl2;
  }
}

export const bluetoothAvailable = () => "bluetooth" in navigator;

/** Ask the user to pick the screen. Must be called from a click handler. */
export async function pickScreen() {
  return navigator.bluetooth.requestDevice({
    filters: [{ name: "Zoom75 Tiga" }, { services: [SERVICE] }],
    optionalServices: [SERVICE],
  });
}

class Inbox {
  constructor() { this.queue = []; this.waiters = []; }
  push(bytes) {
    const w = this.waiters.shift();
    w ? w(bytes) : this.queue.push(bytes);
  }
  next(ms) {
    if (this.queue.length) return Promise.resolve(this.queue.shift());
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => {
        this.waiters = this.waiters.filter((w) => w !== done);
        reject(new Error("timeout"));
      }, ms);
      const done = (b) => { clearTimeout(t); resolve(b); };
      this.waiters.push(done);
    });
  }
  clear() { this.queue = []; }
}

async function waitFor(inbox, expected, ms, what) {
  const need = expected.slice();
  const deadline = performance.now() + ms;
  while (need.length) {
    const left = deadline - performance.now();
    if (left <= 0) throw new Error(`no answer while ${what}`);
    let msg;
    try { msg = await inbox.next(left); } catch { throw new Error(`no answer while ${what}`); }
    const i = need.findIndex((e) => sameBytes(e, msg));
    if (i >= 0) need.splice(i, 1);
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/**
 * Upload a prepared stream. onProgress({ phase, block, blocks, seconds }).
 * Throws UploadError; error.afterControl2 tells whether the screen may be left stuck.
 */
export async function upload(device, stream, { onProgress = () => {}, paceMs = 2, blockTimeoutMs = 2500 } = {}) {
  const chunks = chunkStream(stream);
  const blocks = chunks.filter((c) => c.endsBlock).length;
  const inbox = new Inbox();
  const onNotify = (e) => inbox.push(new Uint8Array(e.target.value.buffer.slice(0)));

  onProgress({ phase: "connecting", block: 0, blocks });
  let server;
  try {
    server = await device.gatt.connect();
  } catch (e) {
    throw new UploadError(`Couldn't connect: ${e.message}. Is the keyboard's Bluetooth on and Pocket Wuque disconnected?`);
  }
  try {
    let service;
    try { service = await server.getPrimaryService(SERVICE); } catch {
      throw new UploadError("That device has no screen-upload service. Pick the entry named exactly \"Zoom75 Tiga\" (not BT1/2/3).");
    }
    const [control, notify, bulk, notify2] = await Promise.all([1, 2, 3, 4].map((n) => service.getCharacteristic(CHAR(n))));
    for (const ch of [notify, notify2]) {
      ch.addEventListener("characteristicvaluechanged", onNotify);
      await ch.startNotifications();
    }
    await sleep(150);
    inbox.clear();

    onProgress({ phase: "handshake", block: 0, blocks });
    try {
      await control.writeValueWithoutResponse(control1For(stream));
      await waitFor(inbox, C1_ACKS, 1500, "starting the upload (CONTROL1)");
    } catch (e) {
      throw new UploadError(`${e.message}. Nothing was changed on the screen.`);
    }
    await sleep(60);

    // ---- point of no return: the keyboard now shows its upload animation ----
    const started = performance.now();
    try {
      await control.writeValueWithoutResponse(CONTROL2);
      await waitFor(inbox, C2_ACKS, 1500, "starting the transfer (CONTROL2)");
      await sleep(60);
      let block = 0;
      for (const c of chunks) {
        await bulk.writeValueWithoutResponse(c.value);
        if (paceMs) await sleep(paceMs);
        if (c.endsBlock) {
          await waitFor(inbox, [PROGRESS], blockTimeoutMs, `waiting for block ${block + 1} of ${blocks}`);
          block++;
          onProgress({ phase: "sending", block, blocks, seconds: (performance.now() - started) / 1000 });
        }
      }
      onProgress({ phase: "finishing", block, blocks });
      await waitFor(inbox, [COMPLETE], 8000 + blocks * 100, "waiting for the keyboard to finish");
    } catch (e) {
      throw new UploadError(e.message, { afterControl2: true });
    }
    const seconds = (performance.now() - started) / 1000;
    onProgress({ phase: "done", block: blocks, blocks, seconds });
    return seconds;
  } finally {
    try { server.disconnect(); } catch { /* already gone */ }
  }
}
