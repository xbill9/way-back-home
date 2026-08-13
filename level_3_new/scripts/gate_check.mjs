// Exercise the near-field gate in frontend/public/audio-processor.js against
// synthetic signals, outside a browser. Free, offline, ~instant:
//
//     node scripts/gate_check.mjs
//
// It loads the real worklet file, so it tests shipped code rather than a copy.
// This is the only automated check that can see the gate at all -- the Python
// harness writes to the WebSocket directly and never runs the browser's audio
// path, and `make test` stubs the model entirely.
//
// What it CANNOT do is prove the gate works on a real voice in a real room. An
// earlier version passed every check here and then held itself shut through a
// whole round on a real microphone. That is why the checks below are mostly
// about failure modes -- above all, that a gate which cannot decide must fail
// OPEN (degrading to always-on) rather than shut (degrading to a dead mic).
//
// Ratios are measured after the warmup window, since the gate deliberately
// sends everything while its noise floor is still forming.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const src = fs.readFileSync(
    path.join(here, '..', 'frontend', 'public', 'audio-processor.js'), 'utf8');

// Minimal AudioWorklet globals.
let now = 0;
globalThis.sampleRate = 16000;
Object.defineProperty(globalThis, 'currentTime', { get: () => now / 1000 });
globalThis.registerProcessor = () => {};
globalThis.AudioWorkletProcessor = class {
    constructor() { this.port = { postMessage: () => {}, onmessage: null }; }
};

const AudioProcessor = eval(src.replace('registerProcessor', '//') + '; AudioProcessor');

const BLOCK_MS = (128 / 16000) * 1000;   // 8ms
const SECONDS = (n) => Math.round((n * 1000) / BLOCK_MS);
const WARMUP_SKIP = SECONDS(2);          // ignore warmup when scoring

function block(amplitude, n = 128) {
    return Float32Array.from({ length: n }, () => (Math.random() * 2 - 1) * amplitude);
}

function run(label, levels) {
    const p = new AudioProcessor({ processorOptions: { gate: true } });
    const sent = [];
    now = 0;
    for (const amp of levels) {
        now += BLOCK_MS;
        sent.push(p.shouldSend(block(amp)));
    }
    const scored = sent.slice(WARMUP_SKIP);
    const open = scored.filter(Boolean).length;
    const ratio = open / scored.length;
    console.log(
        `${label.padEnd(36)} ${(ratio * 100).toFixed(0).padStart(3)}% of steady state sent` +
        `  (floor ${p.noiseFloor.toFixed(4)})`
    );
    return ratio;
}

const N = SECONDS(10);
const at = (levels) => Array.from({ length: N }, (_, i) => levels(i / N));

const a = run('silence only', at(() => 0.002));
const b = run('steady room chatter', at(() => 0.03));
const c = run('chatter + near-field speech', at((t) => (t > 0.4 && t < 0.7 ? 0.25 : 0.03)));
const d = run('very loud room (must fail OPEN)', at(() => 0.09));
const e = run('speech after a long noisy stretch', at((t) => (t > 0.75 ? 0.25 : 0.03)));
const f = run('quiet room, one short phrase', at((t) => (t > 0.5 && t < 0.62 ? 0.15 : 0.003)));

console.log();
const checks = [
    [a < 0.05, 'silence stays gated'],
    [b < 0.10, 'steady room noise stays gated'],
    [c > 0.25 && c < 0.60, 'speech opens the gate, and only around the speech'],
    [d > 0.90, 'a room above the floor cap fails OPEN, never shut'],
    [e > 0.15, 'speech still opens the gate after a long noisy stretch'],
    [f > 0.05 && f < 0.35, 'a quiet room sends only the phrase'],
];
let failed = 0;
for (const [ok, name] of checks) {
    console.log(`${ok ? 'PASS' : 'FAIL'} ${name}`);
    if (!ok) failed++;
}
process.exit(failed ? 1 : 0);
