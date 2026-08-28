# DESIGN.md — compute.pangle.online (recorded from the shipped build, 2026-08-23)

## The world
A field station at the edge of a pond, at night. Everything follows from that one
sentence: the darkness is a green-black (never pure black), the lights are fireflies
over water, the data reads like instruments, the prose reads like a field journal,
and the frog is the resident bioindicator, not a mascot sticker.

## Tokens (as shipped, in :root)
- `--stock #0C110E` page ground · `--raised #121814` raised band (frog section, fees)
- `--ink #E8EDE9` primary text · `--ink2 #A8B3AA` secondary (≈9:1 on stock, ≈8.5:1 on raised)
- `--hair rgba(232,237,233,.14)` every hairline
- `--amber #F0A868` THE accent: display emphasis, CTAs, chips-active, hover. ≈9.6:1 on stock.
- `--phos #7FD99A` RESERVED: only ever marks live/idle semantics (legend live line,
  idle table ticks, the idle numeral, the agents diagram box). Green NEVER decorates.
  This law was violated twice during the build (OG line, deck chip) and fixed both times.

## Type
- Erode 400/600 (Fontshare, self-hosted woff2) for display + field-note prose.
- Martian Mono variable (Google, self-hosted, latin subset) for EVERY number, label,
  table cell, nav link, and code block.
- Body copy: system sans stack, deliberate. All families end in a generic.
- Radius law: everything sharp except pill chips/buttons (999px). Nothing in between.

## The one memorable moment
The hero pond canvas (`startPond()` in index.html): one mote per live offer from
/api/stats (1:N sampling above 600, legend says so), phosphor = idle share, fireflies
biased LOW toward a waterline at 66% viewport height, reflections stretched, rippled
(sin wobble) and fading with depth, a 1px specular gradient marking the line, and the
frog-observer photo screen-blended at the right edge of the water. `mix-blend-mode:
screen` is what makes the black-background photo sit in the green dark without a seam;
a hard-edged attempt without it looked like a pasted rectangle and was rejected.
rAF pauses on IntersectionObserver exit + visibilitychange; reduced-motion draws one
static frame. Everything else on the page is deliberately quiet to let this land.

## Layout families (each used once, on purpose)
full-bleed canvas hero → full-width live data surface → narrow schematic + stacked
steps → split editorial spread (photo right on desktop, photo first on mobile) →
stacked prose + code panels → manifesto strip → footer. #how and #fees share the main
1180px grid's left edge (a centered-narrow first draft made the left margin jump; fixed
per finish review).

## Honesty rules baked into the UI
No number on the page that did not come from the live API. Skeleton loaders shaped
like the table. Empty and error states written for real. STALE flags surface provider
lag; a dead provider shows as down, never silently hidden. The idle numeral is
computed from interruptible listings and the copy says exactly that.

## Assets
- `img/frog-observer.webp` — Pexels, commercial OK, mapping in img/ATTRIBUTION.md.
- Fonts self-hosted in fonts/ (59KB total). Grain = baked feTurbulence data-URI tile.
- og.png rendered from og.html (1200×630) via render.py; favicon.svg = lilypad glyph.

## Unlisted pages
/moat (moat notes) and /deck (slide 1 ONLY, a deliberate constraint) — same tokens,
noindex, absent from sitemap and nav. Deck uses a left scrim over the frog for text
legibility.

## Traps for the next session
- The reviewer's finish pass caught: default table monotony (now capped 3 rows per
  model client-side), the ", RU" region artifact (ingest-side strip + client guard),
  the how-SVG being illegible at 390 (hidden < 640px, steps carry).
- StaticFiles serves /moat via 307 → /moat/. Fine; don't "fix" it.
- Any CSS/JS change: re-run the drill script (scratchpad/drive.py pattern) — overflow,
  console, document.fonts.check, filter drills, reduced-motion — before shipping.

## Addendum 2026-08-23 (the "crazy effects" escalation)
The hero is now a real-time WebGL scene (`web/pond-gl.js`, WebGL1, two passes):
scene FBO (frog cutout quad + one point-sprite mote per live offer) → screen pass
(gradient night; scene composited above a DYNAMIC waterline computed from the DOM
text block's bottom edge; below it the scene AND a texture of the real headline
words reflect through rippling water: 3-sine wobble + up to 10 ripple rings from
cursor moves/clicks/firefly touches + ambient rings; specular shimmer band at the
line). Reflection uses an EXPANSION factor 1.45 (the water band is shallower than
the headline is high; expansion is also what a glancing angle really does).
- Frog: `img/frog-cut.webp` alpha matte (Pillow luma key + smooth core fill; the
  earlier mix-blend-mode hack is dead). Shader clamps alpha<0.05 and dissolves the
  photo's cropped left/bottom edges. Deck uses the same cutout.
- Fallback chain: reduced-motion → 2D static frame; no WebGL → the original 2D
  canvas pond (still in index.html); DOM frog img shows only in fallback (.gl-on hides it).
- **preserveDrawingBuffer: true is LOAD-BEARING** — without it, headless capture
  and user screenshots race the compositor and can grab a blank canvas (cost us an
  hour; the scene was "invisible" while rendering perfectly).
- Verified: 61fps, water motion 8k px/700ms ambient, click ripple doubles it,
  21-drill suite green, no-WebGL fallback drill green. window.__pondFrames = loop liveness.
- Scroll choreography: .rv reveals (IO, staggered), idle numeral count-up (race-safe
  via idleWanted/idleShown), #how SVG self-draw (pathLength=1 dashoffset), market
  row cascade. All collapse under prefers-reduced-motion.
- Referrals live in the fee seam: Vast param is **ref_id** (not ref), RunPod is ref.

## The tape (added 2026-08-23, Stage 1)
Price-history section after #market. Chart series palette — validated with the dataviz
six-checks on surface #0C110E (all pass; worst pair ΔE 19.0 normal / 9.9 tritan):
vast **#5E8FD9** · runpod **#C97A3C** · datacrunch **#A873BD**. Fixed entity-bound
assignment, never re-ordered. Phosphor stays reserved for idle/live semantics and
amber stays selection/emphasis — neither is ever a series colour. Time is UTC
everywhere on the tape (axis, tooltip, table) — the box is +0800 and mixed clocks
lie. Tooltip lists EVERY provider at the crosshair (per-provider snapshot ts differ
by seconds; union timestamps are clustered to tol = bucket/2 or 240s). Direct labels
at line ends + legend; table view under a <details>; coverage note admits how much
tape exists vs the window. Everything is textContent, no innerHTML on data.

## The tripwire (added 2026-08-23, Stage 1)
Watch section after #tape: one wrapping form row (pill inputs matching the chip
radius law), result rendered as mono journal lines — armed in phosphor (a live
state, so the reserved green is correct), refusals in amber. No account: the
watch id IS the key, said plainly. Webhooks refuse private/loopback targets at
create and at send (the box hosts internal services; SSRF is the attack). All
output DOM is textContent.

## The readings (added 2026-08-24, intel UI)
Section #readings between #tape and #wire: a 2×2 hairline instrument grid (a new
layout family — broadsheet panels, stacks <860px), lazy-loaded via IO at 400px margin.
Four instruments over /api/value /spread /timing /reliability, each carrying its API's
own honesty line + a link to the raw endpoint, and a written error state ("Instrument
offline — /api/x did not answer").
- Value: ranked graphite bars (rgba ink .30 — deliberately monochrome; magnitude only),
  value at tip, phosphor idle-tick dot marks interruptible rows (same convention as the
  market table; `.im .idle-tick` shares the td rule).
- Idle discount: phosphor-filled bars — the ONE data use of phosphor here, legal because
  the measure IS idle semantics (what idle capacity saves).
- Timing: tape-select + class chips → serif verdict sentence, 30d range track with 7d
  inner band and an amber "now" dot (amber = the emphasized current reading), percentile
  figures in mono. Descriptive-not-forecast stated in the note.
- Feeds: mono table, phosphor dot ≥99 score / amber below (stale convention), machine avg
  only where a provider publishes one (vast), em-dash otherwise.
Colors are per-ROLE (site tokens), not a categorical series palette — no two hues ever
encode adjacent series identity, every value is direct-labeled.
