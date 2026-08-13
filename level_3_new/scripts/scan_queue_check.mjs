// Exercise the scan scheduler in frontend/src/scanScheduler.js. Free, offline,
// instant:
//
//     node scripts/scan_queue_check.mjs
//
// The scheduler decides whether a scan interrupts the model or waits for it to
// finish. The two ways to get that wrong are opposite and both bad -- interrupt
// everything (clipped speech, the behaviour this replaced) or hold too long
// (lag, which is worse than clipping). These cases pin both ends.
import { ScanScheduler } from '../frontend/src/scanScheduler.js';

let failures = 0;
function check(name, ok, detail = '') {
    console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${detail ? `  (${detail})` : ''}`);
    if (!ok) failures++;
}

function make(startAt = 0) {
    let clock = startAt;
    const sent = [];
    const s = new ScanScheduler({
        send: () => sent.push(clock),
        now: () => clock,
    });
    return { s, sent, advance: (ms) => { clock += ms; } };
}

// Silence: straight out, no delay at all. This is the common case and must not
// have acquired any latency.
{
    const { s, sent } = make();
    const outcome = s.request();
    check('a scan during silence sends immediately', outcome === 'sent' && sent.length === 1);
}

// Mid-reply: held, then released the moment the turn ends.
{
    const { s, sent, advance } = make();
    s.onAudio();
    advance(100);
    const outcome = s.request();
    check('a scan during a reply is held', outcome === 'held' && sent.length === 0);
    advance(900);
    s.onTurnComplete();
    check('...and fires as soon as the reply ends', sent.length === 1, `held ${sent[0] - 100}ms`);
}

// Held time tracks the reply, so a short reply means a short wait.
{
    const { s, sent, advance } = make();
    s.onAudio();
    s.request();
    advance(300);
    s.onTurnComplete();
    check('a short reply means a short hold', sent.length === 1 && sent[0] === 300, `${sent[0]}ms`);
}

// Repeats collapse: "scan scan scan" over a reply is one scan afterwards.
{
    const { s, sent, advance } = make();
    s.onAudio();
    s.request(); advance(200); s.request(); advance(200); s.request();
    advance(200);
    s.onTurnComplete();
    check('repeated requests during one reply collapse to a single scan', sent.length === 1, `${sent.length} sent`);
}

// Backstop: a turn that never completes must not strand the request.
{
    const { s, sent, advance } = make();
    s.onAudio();
    s.request();
    advance(1999);
    s.tick();
    check('the backstop has not fired early', sent.length === 0);
    advance(2);
    s.tick();
    check('a turn that never completes still releases the scan', sent.length === 1);
}

// A new session starts clean: nothing held over from the last one.
{
    const { s, sent, advance } = make();
    s.onAudio();
    s.request();
    s.reset();
    advance(5000);
    s.tick();
    s.onTurnComplete();
    check('reset drops anything held from a previous session', sent.length === 0);
}

process.exit(failures ? 1 : 0);
