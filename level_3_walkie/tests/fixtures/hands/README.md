# Hand fixtures

Ground truth for `scripts/scan_accuracy.py`. `hand_N.jpg` shows exactly N
extended fingers.

Synthetic: generated on 2026-08-13 with `gemini-3.1-flash-lite-image` via the
Interactions API, then downscaled to **640x480 JPEG quality 60** — the exact
format `useGeminiSocket.js` puts on the wire, so the harness feeds the backend
frames it cannot distinguish from a browser's.

**Every count was checked by eye before the files were committed.** Generative
image models are unreliable at hands specifically, so the labels are not taken
on trust from the prompts that produced them. Re-verify by looking at the images
if you ever regenerate them; a mislabelled fixture turns the harness into a
confidently wrong instrument.

What they are: sharp, well-lit, hand-fills-frame, plain background, one hand per
image. That makes them the *easy* case. A run that fails here is unambiguous; a
run that passes says less than a run by hand, which is why the harness has
`--blur-prob` and `--jitter` to approximate a real capture.
