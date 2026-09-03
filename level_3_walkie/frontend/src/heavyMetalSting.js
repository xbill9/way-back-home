/**
 * A short distorted power chord, synthesised on the fly.
 *
 * This replaces a hotlinked archive.org mp3 that returns 404 -- so the Devil's
 * Horns easter egg had no audio payoff at all, it just logged a failed fetch.
 * Generating the sting locally also means it works offline, survives a
 * restrictive CSP, and carries no licensing question, which a bundled copy of
 * someone's recording would.
 *
 * E5 power chord (E2 + B2 + E3), sawtooth through a waveshaper for grit, with a
 * fast attack and a long-ish decay.
 */

const FREQUENCIES = [82.41, 123.47, 164.81]; // E2, B2, E3
const DURATION = 2.4;

/** Cubic soft-clip curve -- cheap, stable distortion. */
function distortionCurve(amount = 60) {
    const samples = 1024;
    const curve = new Float32Array(samples);
    const deg = Math.PI / 180;
    for (let i = 0; i < samples; i++) {
        const x = (i * 2) / samples - 1;
        curve[i] = ((3 + amount) * x * 20 * deg) / (Math.PI + amount * Math.abs(x));
    }
    return curve;
}

export async function playHeavyMetalSting() {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;

    const ctx = new Ctx();
    // Autoplay policy: this runs from a gesture-triggered state change, but the
    // context can still start suspended.
    if (ctx.state === 'suspended') await ctx.resume();

    const now = ctx.currentTime;

    const shaper = ctx.createWaveShaper();
    shaper.curve = distortionCurve();
    shaper.oversample = '4x';

    // Roll off the fizz the waveshaper adds up top.
    const tone = ctx.createBiquadFilter();
    tone.type = 'lowpass';
    tone.frequency.value = 2600;

    const master = ctx.createGain();
    master.gain.setValueAtTime(0.0001, now);
    master.gain.exponentialRampToValueAtTime(0.32, now + 0.02); // pick attack
    master.gain.exponentialRampToValueAtTime(0.18, now + 0.5);  // sustain
    master.gain.exponentialRampToValueAtTime(0.0001, now + DURATION);

    shaper.connect(tone);
    tone.connect(master);
    master.connect(ctx.destination);

    const oscillators = FREQUENCIES.flatMap((freq) =>
        // Two slightly detuned voices per note: that beating is most of what
        // makes a guitar sound like a guitar rather than a synth.
        [-6, 6].map((detune) => {
            const osc = ctx.createOscillator();
            osc.type = 'sawtooth';
            osc.frequency.value = freq;
            osc.detune.value = detune;
            osc.connect(shaper);
            osc.start(now);
            osc.stop(now + DURATION);
            return osc;
        })
    );

    // Release the hardware once the sting has rung out.
    const last = oscillators[oscillators.length - 1];
    last.onended = () => ctx.close().catch(() => {});
}
