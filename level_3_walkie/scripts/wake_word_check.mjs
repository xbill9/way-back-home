// Exercise the wake-word listener in frontend/src/wakeWord.js. Free, offline,
// instant:
//
//     node scripts/wake_word_check.mjs
//
// One spoken "scan" must send exactly one scan. Chrome delivers an utterance
// several times over -- interim results refined until the final one lands, all
// sharing a result index -- and every extra fire is a second billed scan whose
// report_digit round-trip delays the spoken confirmation by ~0.7s under
// BLOCKING. That was measured in a real session: two `USER TEXT: scan` frames
// 1.03s and 1.44s apart, slipping past the 1s timer that used to be the only
// guard.
//
// The opposite failure matters just as much: dedup so eagerly that a second,
// genuine "scan" is swallowed and the scanner ignores you.
import { WakeWordListener } from '../frontend/src/wakeWord.js';

let failures = 0;
function check(name, ok, detail = '') {
    console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${detail ? `  (${detail})` : ''}`);
    if (!ok) failures++;
}

// A stand-in for Chrome's SpeechRecognition: records the handlers the listener
// installs so the test can deliver results the way the browser does.
class FakeRecognition {
    constructor() { FakeRecognition.last = this; }
    start() { this.started = true; }
    stop() { this.started = false; }
}

let clock = 0;
globalThis.performance = { now: () => clock };
globalThis.window = { SpeechRecognition: FakeRecognition };

function make() {
    const fired = [];
    const listener = new WakeWordListener(['scan'], { onCommand: (t) => fired.push(t) });
    listener.start();
    const rec = FakeRecognition.last;
    // `results` is cumulative and indexed by utterance, exactly as the browser
    // reports it; `resultIndex` is where the new material starts.
    const results = [];
    const deliver = (index, transcript) => {
        results[index] = [{ transcript }];
        rec.onresult({ resultIndex: index, results });
    };
    return { fired, rec, deliver };
}

// One utterance, refined. The browser emits the same index repeatedly.
{
    clock = 0;
    const { fired, deliver } = make();
    deliver(0, 'sc');
    deliver(0, 'scan');
    deliver(0, 'scan');
    check('an utterance refined three times sends one scan', fired.length === 1,
        `sent ${fired.length}`);
}

// The regression this exists for: the refinement arriving after the old 1s
// timer had expired. Same utterance, so still one scan.
{
    clock = 0;
    const { fired, deliver } = make();
    deliver(0, 'scan');
    clock = 1030;
    deliver(0, 'scan');
    clock = 1440;
    deliver(0, 'scan it');
    check('a refinement 1.4s later is still the same utterance', fired.length === 1,
        `sent ${fired.length}`);
}

// Saying it again must work, or the scanner goes deaf after one scan.
{
    clock = 0;
    const { fired, deliver } = make();
    deliver(0, 'scan');
    clock = 4000;
    deliver(1, 'scan');
    check('a second utterance sends a second scan', fired.length === 2,
        `sent ${fired.length}`);
}

// Recognition restarts constantly (it stops itself on silence) and numbers its
// results from 0 again, so index 0 must not be remembered across a restart.
{
    clock = 0;
    const { fired, rec, deliver } = make();
    deliver(0, 'scan');
    rec.onend();
    clock = 4000;
    deliver(0, 'scan');
    check('the first command after a restart still sends', fired.length === 2,
        `sent ${fired.length}`);
}

// Non-matching speech is not a command, however much of it there is.
{
    clock = 0;
    const { fired, deliver } = make();
    deliver(0, 'what a nice day');
    clock = 4000;
    deliver(1, 'hello there');
    check('unrelated speech sends nothing', fired.length === 0, `sent ${fired.length}`);
}

console.log(failures ? `\n${failures} FAILED` : '\nall passed');
process.exit(failures ? 1 : 0);
