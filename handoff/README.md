# Handoff: wisper-transcribe UI redesign (Studio direction)

> Replace the placeholder Tailwind UI in `src/wisper_transcribe/web/templates/` with the **Studio** design direction shown in the bundled prototype.

---

## 1. About these files

The files in this bundle are **design references created in HTML**. They are clickable prototypes that show the intended look and behavior — **not production code to copy directly**. Your job is to recreate these designs in the existing wisper-transcribe codebase, working with its established stack:

- **FastAPI + Jinja templates** at `src/wisper_transcribe/web/`
- **Tailwind CSS** (pre-built at `static/tailwind.min.css`)
- **htmx** for live updates (job queue polling, Discord recording SSE)
- **Vanilla JS** in `static/app.js` for any extra behavior

Keep the architecture. Replace the styling and information design.

---

## 2. Fidelity

**High-fidelity.** Every screen uses final colors, typography, spacing, and copy. Recreate pixel-perfectly using Tailwind utility classes + a small set of CSS custom properties or `tailwind.config.js` extensions for the design tokens listed below.

---

## 3. How to preview

Open `prototype-standalone.html` in any browser — it's a single-file, fully offline copy of the clickable prototype.

The unbundled version (`prototype.html` + `screens/` + `prototype.jsx`) is also included so you can read the inline-styled JSX as a source-of-truth for spacing, sizing, and color values. **The JSX is for reference only — do not ship React.**

---

## 4. The chosen direction: "Studio"

The wisper logo (deep navy + glowing cyan/teal quill, scroll, d20, will-o-wisp) suggested a darker, atmospheric UI. We explored four directions and landed on **Studio**:

- **Structure of a power-user tool**: persistent left sidebar, dense data tables, monospace technical fields, terminal-style live log feeds.
- **Content typography of an archive**: Newsreader serif italic for titles and pull-quotes, hairline rules instead of heavy boxes, drop caps in long-form summary copy.
- **Restrained color**: paper-cream text on near-black navy, with **cyan as the only saturated accent**, reserved for live/active states (recording indicator, active job, currently-playing speaker, current tab).

This mirrors apps like Linear, Plex, and Sonarr in chrome, but pulls the editorial mood from the logo for the content surfaces.

---

## 5. Design tokens

All values are exact. Put these in `tailwind.config.js` `theme.extend` (or CSS custom properties on `:root`).

### Colors

| Token | Hex | Use |
|---|---|---|
| `bg` | `#0b0f17` | Page background — near-black with hint of navy |
| `bgRaised` | `#11161f` | Cards, hover row backgrounds, sidebar active |
| `bgRaised2` | `#161c26` | One step above raised (selected items inside cards) |
| `bgSunken` | `#080b12` | Sidebar background, log feeds, code blocks |
| `rule` | `rgba(243, 234, 216, 0.09)` | Default hairline border — cream-tinted at low alpha |
| `ruleStrong` | `rgba(243, 234, 216, 0.18)` | Emphasized hairline (selected items, dashed drop zones) |
| `text` | `#f3ead8` | Paper-cream — primary text |
| `textBright` | `#fff8e8` | Hover/emphasis on already-light text |
| `textDim` | `#a3a89e` | Secondary text |
| `textFaint` | `#5f6571` | Tertiary text, labels, mono kickers |
| `cyan` | `#5fd4e7` | THE accent. Live states only. |
| `cyanDeep` | `#2a8da0` | Hover/pressed cyan |
| `green` | `#7bd88f` | Healthy/success states (GPU ready, refined badge) |
| `amber` | `#e4b572` | Warnings, "Unknown" / "Tense" / "Failed" |
| `rose` | `#e88b8b` | Recording state, "Missing", danger destructive |
| `violet` | `#a78bfa` | Rarely used — secondary semantic only |

### Typography

- **Serif (display)** — `Newsreader` from Google Fonts, weights 300/400/500, italic available. Used for:
  - Page titles ("Kings and Queens", "The desk", "The archive")
  - Section titles paired with mono kickers ("I · Recap" / "The thread")
  - Pull quotes (italic)
  - Drop caps in long copy
  - Numbered lists in italics for follow-ups
- **Sans (body)** — `Geist` from Google Fonts, weights 300/400/500/600/700. Used for everything else.
- **Mono (technical)** — `JetBrains Mono` from Google Fonts, weights 400/500/600. Used for:
  - All IDs (`j_8a1f`, `r_a8f1c2`)
  - All timestamps (`21:51:44`, `02:14:22`)
  - All CLI flags (`--num-speakers 5`)
  - Kicker labels (uppercase, letter-spacing 0.15em)
  - Status pills ("RECORDED", "TRANSCRIBED")
  - Stat block kickers
  - File paths
- Base font size: **14px**. Line height 1.5. Don't go smaller than 12px anywhere.
- Type pairings:
  - Tab labels: mono, uppercase, 0.08em tracking
  - Status badges: mono, uppercase, 0.12em tracking, 2px×7px padding, 1px border with matching color at 40% alpha
  - Section section kicker: mono, uppercase, 10px, 0.18em tracking, `textFaint`
  - Section section title (right next to kicker): serif italic, 20–24px, weight 400, `text`

### Spacing

The system is hairline-driven, not card-driven. Most sections are separated by `1px solid rule` lines, not boxes.

- Container padding: `20px 28px` for content area, `24px 28px` for hero sections, `20px 14px` for sidebar.
- Row padding: `14px 0` for table-like rows separated by hairlines.
- Card padding (when used): `20px 22px`.
- Section heading bottom margin: `14–16px`.
- Section bottom margin (between sections): `28–36px`.
- Gap between sibling cards: `14px`.
- Gap between grid columns: `28–36px` on content areas, `16px` between cards.

### Borders & radii

- Border width: always `1px`. No 2px borders anywhere.
- Border radii:
  - Tables/lists: **0** (square corners — hairline rules don't need radii)
  - Buttons: `6px`
  - Cards: `4–8px`
  - Avatars: 50% (round)
  - Pills/badges: `3–4px` for rectangular pills, `999px` for pill chips

### Shadows / glow

Used sparingly to flag live state:

- Active cyan dot: `0 0 6px <cyan>`
- Recording rose dot: `0 0 12px <rose>, 0 0 24px <rose>60`
- Live element halo (Priya speaking): `0 0 0 2px <cyan>80, 0 0 14px <cyan>40` on avatar
- Stop button: `0 0 16px <rose>50`
- Otherwise: no shadows. The design is flat with hairlines.

---

## 6. Screens

### Common chrome (every screen)

#### Sidebar (`StudioSidebar` in `screens/studio.jsx`)

- 204px fixed width, `bgSunken` background, right border `1px rule`.
- Top: 30×30 logo (`assets/logo.png`, 7px radius) + `wisper` in serif italic 18px + `v0.7.2` in mono 9px below, separated from items by `1px rule` with 22px bottom padding.
- Nav items (in order): Dashboard, Transcribe, Record, Transcripts, Speakers, Campaigns, Config.
  - Each: 14px icon + label, 8/10px padding, 6px radius.
  - Active: `bgRaised` background, 2px cyan left border (`paddingLeft` adjusts from 10→8 to maintain alignment), label in `text`, icon in `cyan`.
  - Inactive: `textDim` label, `textDim` icon, transparent background.
- Bottom: system status block in mono 10px (GPU, VRAM, JOBS counts) above a user row (avatar circle + username + `localhost:8080` server hint). Both separated by `1px rule`.

#### Toolbar (`StudioToolbar` in `screens/studio.jsx`)

- Height ~70px, `14px 24px` padding, bottom `1px rule`.
- Left: mono kicker (10px uppercase 0.15em tracking, `textFaint`) ABOVE the page title in serif italic 22px.
- Optional sub: mono 11px, `textFaint`, beside the title.
- Right: search input (240px, mono kbd hint `⌘K`), plus 1–2 action buttons.
- Action button pair pattern: secondary `bgRaised + rule` outline button, then a primary action — cyan filled (`bg` text on it) with a 20px cyan glow `boxShadow`.

#### Tabs (Transcript detail, Config sections)

- Mono 12px uppercase, 0.08em tracking.
- 2px bottom border on active tab in `cyan`. Inactive 2px transparent.
- Each tab: 11px×14px padding, optional 11px icon to the left.
- Optional badge after tab name (e.g. "DM notes" beside Summary): 9px mono, `cyan` text, 1px cyan@40% border.

### Screen 1 — Dashboard

**Route:** `GET /` (existing `dashboard.py`)

**Purpose:** Landing — see active jobs at a glance, jump into anything.

**Layout:**
1. Hero: full-width banner under chrome, bottom `1px rule`. Serif 300 weight 38px headline mixing italic for the second clause ("Two sessions are processing. *Theo is still missing.*"). 24px bottom margin. The italic clause is a one-liner tailored to current state (job count, missing speakers, etc.).
2. Stats strip: 6-column grid (`grid-cols-6`), no gaps, `1px rule` top/bottom borders, `1px rule` left border on items 2–6. Each cell: 18px padding, kicker→large serif numeral (32px 300 weight)→sub. First stat ("In progress") gets a cyan dot indicator + cyan numeral when there's an active job.
3. Two-column body: left column 1.6fr (jobs + archive), right column 1fr (campaigns + speakers).

**Left column:**
- **Now processing** section → "The desk" serif title. Table grid with columns `12px / 1.7fr / 110px / 1fr / 70px / 56px`:
  - Status dot (cyan + glow if active; outline circle if queued)
  - Session filename in serif 15px, job ID + start time in mono below
  - Campaign indicator (5px colored dot + first word)
  - Stage label + 2px-tall progress bar (cyan with glow when active)
  - ETA (mono right-aligned)
  - Percent (mono right-aligned, cyan if active)
- Followed by a log strip in `bgSunken` (the live tqdm tail) — mono 11px lines, columns: timestamp / job ID / message. Job IDs colored cyan for the active job, amber for refine.
- **Recent** section → "The archive". Same columns minus the status dot. Click any row → transcript detail.

**Right column:**
- **Campaigns** → "The tables". Per campaign: 3px vertical color rule (left), serif name, mono system, large serif session count right-aligned with "SESSIONS" mono caption.
- **Speakers** → "The voices". Per speaker: 26px colored circle initial avatar, serif name, mono ROLE caption, session count right.

**Interactions:**
- "view queue →" link → `/transcribe` (active jobs view)
- "archive →" → `/transcripts`
- "+ NEW" → `/campaigns?new=1`
- "manage →" → `/speakers`
- Each archive row clickable → `/transcripts/<slug>` 
- Each campaign row clickable → `/campaigns/<slug>`
- Each speaker row clickable → `/speakers#<name>`
- Toolbar "Transcribe" → `/transcribe`
- Toolbar "Start session" → `/record`

### Screen 2 — Transcribe

**Route:** `GET /transcribe` (existing `transcribe.py`)

**Purpose:** Submit a new transcription job and watch it run.

**Layout:** Two-column grid `1.55fr / 1fr`.

**Left column:**
1. **Source section** with kicker → "Add a session". A 1px dashed `ruleStrong` drop zone with subtle vertical cyan gradient at low alpha. Center: 52px circular outlined icon ("waveform"). Below: serif italic 22px prompt "Drop audio here." Below that: small fields hint with cyan underline on "browse files".
2. Selected-file row beneath, a `bgRaised + rule` card with: 14px waveform icon, filename in serif, full path in mono, size + duration in mono, ✕ remove.
3. **Processing now** section. Pipeline strip of 6 stages, `1px rule` top + bottom, hairline-divided columns. Each stage has a dot indicator (green=done, cyan glow=active, outline=pending), uppercase mono name, and a value in either serif (active) or mono (other). Active stage gets a cyan@08 background tint.
4. Overall progress bar (3px tall, full-width) + "38% · ~4 min" mono label.
5. Log feed: `bgSunken` panel, mono 11px, full SSE tail.

**Right column — Settings (sticky):**
- **Campaign** picker: campaign dot + serif name + chevron, with `--campaign` mono CLI flag in the kicker row.
- **Whisper model** segmented control: 3 options, selected option `bgRaised` + cyan text.
- **Speakers**: 10-bar segmented slider with cyan top-border on the selected count, big serif numeral 22px in cyan.
- **After transcription** toggles (Refine, Summarize, Update voice profiles): each shows serif label + mono `--flag` and a sub-description. Toggle is a 28×16 pill (cyan when on with 12px glow).
- **Vocabulary hints** as serif italic chips with `bgRaised` background.
- **Equivalent CLI** preview: `bgSunken` block in mono with `wisper` in cyan, the filename in green.

**Interactions:**
- Toolbar "Run job ↵" — primary cyan button → POST `/transcribe` (htmx form submit).
- All controls are normal Jinja form fields; cluster them in one `<form>` and let the existing route handle it.

### Screen 3 — Record (the marquee live screen)

**Route:** `GET /record` (existing `record.py`)

**Purpose:** Watch a Discord session capture in real time. Spot who's talking. Spot bot problems. Stop cleanly.

**Layout:** Two columns `1fr / 320px`, but with a distinctive top toolbar.

**Toolbar:** background tinted with a subtle rose@10 gradient. Three groups:
- Left: 12px rose dot with double-stacked glow (12px + 24px@60%) + uppercase "RECORDING" in 0.18em rose mono → big mono elapsed time `01:24:11` (26px, weight 500) + "elapsed" caption → vertical 1px rule → "Channel" kicker + serif italic channel name `#table-1 · The Crooked Coffer`.
- Right: secondary buttons "Add marker" (spark icon), "Pause", and a primary **Stop recording** button — rose background, dark text, 16px rose glow, with a 9px square (stop glyph) before the label.

**Main column:**
1. Status strip — 5-column hairline grid (Speakers / Segments / Bot ping / Storage / Markers). Speakers value uses cyan; Bot ping uses green.
2. **At the table** → the per-speaker meters. Hero of this screen.
   - Each speaker row: 36px circular avatar (oklch color from speaker `hue`), serif name 18px, role + Discord ID mono below, a wide meter component, talk time (right-aligned, cyan if currently speaking), and a LIVE/QUIET pill.
   - **The meter**: 28 vertical bars, height-modulated by a per-bar sine wave + speaker `level` coefficient. When speaking: cyan gradient bars with optional glow on tall bars. When quiet: subdued `ruleStrong` bars at ~18% height.
   - Active speaker (e.g. Priya): row background gets a `linear-gradient(90deg, <cyan>06, transparent 60%)`; avatar gets a 2px cyan ring + 14px cyan halo; talk-time turns cyan; LIVE pill replaces QUIET.
3. **The thread** — auto-scrolling live transcript ticker. `bgRaised + rule` panel. Each line is a `60px / 90px / 1fr` grid: timestamp / speaker dot+name / utterance. Newest line at top in `text`, older lines fade to `textDim` with opacity 1 → 0.45. Footer caption: "PARTIAL TRANSCRIPT · WHISPER-STREAMING · 3s WINDOW" in mono with a cyan dot.

**Right sidebar:**
- **This session**: campaign dot+name, episode title, start time, recording_id.
- **Segment manifest**: top-bordered list of recent segments (current "writing" in cyan, sealed segments in textDim). Codec/rotation info below in mono.
- **Markers**: serif italic labels right-aligned timestamps. Click to seek (post-record).
- **When you stop** explainer card: numbered list of what happens on stop.

**Interactions:**
- This screen lives off an EventSource at `/record/sse` (already wired in the existing app). Bind:
  - Speaker dot intensity ← per-frame `voice_activity` events
  - Segment manifest tail ← `segment_sealed` events
  - Ticker ← `partial_transcript` events
- Toolbar Stop → POST `/record/stop`. On stop, redirect to `/recordings/<id>` (the new Recordings detail you may add later) or `/recordings` list.

### Screen 4 — Recordings list

**Route:** `GET /recordings` (existing `record.py`)

**Purpose:** See all raw captures across states.

**Layout:**
1. Filter row of pill buttons: All / Live / Transcribing / Done / Failed. Active pill has `bgRaised` background. Counts in mono. Search input at right.
2. **Live now** group (only renders if there's an active recording). The live recording is a hero card with the rose tint: status pill + duration, serif title, campaign dot/name + channel mono, segment count, voice count in cyan, a 30-bar rose meter graphic, and a primary "Open live view →" button (jumps to `/record`).
3. **Captured** group: data table with columns `12px icon / 1.4fr session / 1fr campaign+channel / 90px duration / 70px segs / 110px status / 130px action`. Status pill colors:
   - `TRANSCRIBED` → green
   - `TRANSCRIBING` → cyan + 2px progress bar below
   - `FAILED` → amber, with the error message in italic mono on a second line
4. Action column:
   - Done → "Open transcript →" (text button) → `/transcripts/<slug>`
   - Transcribing → "view job →" (mono cyan) → `/jobs/<id>` or dashboard
   - Failed → "Retry transcribe" (amber outline button) → POST `/recordings/<id>/transcribe`

### Screen 5 — Transcripts list

**Route:** `GET /transcripts` (existing `transcripts.py`)

**Purpose:** Browse the archive, grouped by campaign.

**Layout:**
1. Filter row: All pill + one pill per campaign (campaign color dot + name + count). Search at right.
2. One section per campaign:
   - Header row: campaign color dot, serif italic campaign name (`The Wildwood`), uppercase mono caption "D&D 5E · 14 EPISODES", hairline rule fills remaining space, "open campaign →" link in cyan mono on the right.
   - Table headers in mono 10px 0.12em tracking: SESSION / DURATION / WORDS / SPEAKERS / STATE / DATE.
   - Rows: 14px scroll icon, serif title, optional "VIEWING" cyan pill on the current transcript, mono numeric columns, status pills (REFINED green, SUMMARY cyan, RAW textFaint), date mono right-aligned.
3. Whole row is clickable → `/transcripts/<slug>`.

### Screen 6 — Transcript detail (Summary view default)

**Route:** `GET /transcripts/<name>` (existing `transcripts.py`)

**Purpose:** Read the DM-facing campaign notes (recap, loot, NPCs, follow-ups). The marquee read-mode screen.

**Layout:**
1. **Breadcrumb bar** (height 60px, 1px rule below): `transcripts / impossible-landscapes / S1E3-kings-and-queens.md` in mono, with the campaign slug clickable. Right side: refined-status mono pill, Listen / Export .md / Re-refine secondary buttons.
2. **Hero** (padded 32px 36px 24px, 1px rule below): campaign dot + uppercase mono trail (CAMPAIGN · SYSTEM · EPISODE), big serif title 56px mixing italic for `and` ("Kings *and* Queens"), meta strip below (DATE / DURATION / WORDS / VOICES / SANITY) in mono kicker→value pairs.
3. **Tabs**: Summary (active, with "DM notes" cyan badge) / Transcript / Speakers / Audio / Raw .md.
4. **Body** — two columns `1.55fr / 1fr`, padding `36px 36px`.

**Main column:**
- **I · Recap** kicker + hairline fill + count caption (`3 PARAGRAPHS · 184 WORDS`). Body is serif 17px line-height 1.65. The first paragraph leads with a **drop cap** — first letter as serif 46px floated left.
- **Pull quote**: 1px rule top + bottom, 24px vertical padding. Italic serif 24px weight 300 with quote marks. Attribution below in mono caption (`— YUKI · 02:14:22`).
- **II · Loot & objects** with `N ITEMS · 3 UNUSUAL` caption. Hairline-separated rows: a 12px glyph (◈ cyan for unusual, ◦ faint for mundane), serif item name, where-found in textDim, MUNDANE/UNUSUAL pill at right. Optional italic serif note below indented under the name.
- **III · Pivotal moment** in a `bgRaised + rule` card showing a 19-second excerpt with timestamp / speaker / utterance per line.

**Sidebar:**
- **Follow-ups** in a cyan-bordered (40% alpha) glowing card with 28px cyan shadow. Numbered list, big serif italic numerals (00, 01, 02, …) and serif italic body. Header: spark icon + uppercase mono "Follow-ups" + "NEXT SESSION" caption right.
- **NPCs / persons**: hairline-separated entries. Serif name + status pill (`MISSING` rose / `UNKNOWN` amber / `ALIVE` green) + uppercase mono role + italic serif note.
- **Voice distribution**: per speaker, colored dot + serif name + horizontal bar (`bgSunken` track, oklch-colored fill matching speaker hue) + mono percent.
- **Refine receipt** — `bgSunken + rule` mono receipt block with `✓ REFINE RECEIPT` in green, vocab/speakers/LLM/completed lines.

**Interactions:**
- Tabs switch view in-page (htmx swap into a partial, like existing `transcript_detail.html`).
- Breadcrumb `transcripts` → `/transcripts`; campaign slug → `/campaigns/<slug>`.
- Listen → opens audio player drawer (out of scope for this redesign — keep existing behavior).
- Re-refine → POST `/transcripts/<name>/refine`.

### Screen 7 — Speakers (voice profiles)

**Route:** `GET /speakers` (existing `speakers.py`)

**Purpose:** Manage enrolled voice profiles.

**Layout:**
1. Intro paragraph in serif italic 16px, max-width 720px: a one-liner explaining how matching works.
2. **2-column grid**, no gaps, with 1px `rule` borders forming a single-line hairline grid. Each cell is a profile card padded `22px 24px`:
   - 56px circular avatar (oklch from speaker hue) with the speaker's initial in serif italic 24px.
   - Top right: ⋯ menu button (textFaint).
   - Serif name 22px + uppercase mono role + Discord ID partial.
   - **64-bar waveform sample** (height 30px, oklch hue at 55% opacity).
   - Stats row: 4 inline cells separated by `1px rule` left borders — SESSIONS / SIM (colored green/cyan/amber by tier) / HEARD / SOURCE.
   - Actions row: small `bgRaised + rule` buttons — Sample (play icon), Bind Discord ID, Re-enroll, then "Remove" rose-colored at far right.
3. **Enroll another voice** card at the bottom — 1px dashed border, a 48px dashed-bordered + icon, serif italic title, sub copy, "Browse audio" and "From last recording" actions.

**Interactions:**
- Sample → plays a clip via existing endpoint.
- Re-enroll → POST `/speakers/<name>/enroll` with the next-up sample.
- Enroll new → POST `/speakers/enroll` (multipart audio).

### Screen 8 — Campaigns

**Route:** `GET /campaigns/<slug>` (existing `campaigns.py`)

**Purpose:** Manage one campaign's roster, defaults, and episode timeline. Includes campaign list on the left for switching.

**Layout:** Two-column `260px / 1fr`:

**Left rail (campaign list):**
- `bgSunken` background, 1px rule right border, 18px 14px padding.
- "ALL CAMPAIGNS · N" mono kicker.
- Each campaign: 12/14 padded card, 3px vertical color rule + serif name + mono system + session count below. Selected campaign card gets `bgRaised` background + 1px ruleStrong border.
- Below: "+ New campaign" dashed-outline full-width button.

**Detail:**
1. Hero: campaign dot + uppercase trail with slug — title in serif 44px, italic flavor paragraph in serif italic 15.5px, stats strip (Sessions / Hours / Words / Players / Last met).
2. Two columns `1.4fr / 1fr`:

**Left — Episodes** ("The thread" serif title, "+ ADD EPISODE" link). Grid `60px / 1fr / 90px / 120px`: episode number mono, serif title, date mono, state pill (LIVE rose with glow or TRANSCRIBED green). Click any row → transcript detail.

**Right column:**
- **Roster** ("At this table" + "+ ADD MEMBER" cyan link). Per member: small avatar + serif name + uppercase mono role + character name in textDim + "● BOUND" green caption when Discord ID is bound.
- **Settings**: hairline rows of `serif italic key` ↔ `mono or sans value`. Includes Discord channel, vocabulary count, default model, output folder, auto-refine, auto-summarize.

### Screen 9 — Config

**Route:** `GET /config` (existing `config.py`)

**Purpose:** All settings.

**Layout:** Two-column `220px / 1fr`, with a left section-nav and the active section's content on the right.

**Left section nav:**
- `bgSunken`, 14px padding.
- "SECTIONS" mono kicker.
- Items: Transcription / LLM / Discord / Storage / About.
- Active: `bgRaised` + 2px cyan left border + serif italic label. Inactive: serif label (non-italic) in textDim.

**Right content** changes per section. All sections share the section heading pattern (kicker + serif title) and use hairline-separated `key / value` rows where:
- Key is serif italic 15px, with optional caption below in textFaint 12px
- Value can be: mono token, cyan/green/amber status text, a `<select>` mocked styled to match (background `bgRaised`, border `ruleStrong`, mono font), or a clickable "view default →" cyan mono link

Section specifics:

- **Transcription**: HF token (masked + rotate link), default whisper model select, compute device, beam size, parallel diarize toggle.
- **LLM**: a 5-tile grid showing each provider (Ollama / LM Studio / Anthropic / OpenAI / Google). Active provider (Ollama) gets cyan border @ 50% alpha + cyan@08 background + "● ACTIVE" caption in cyan; others get `bgRaised` + "○ IDLE" textFaint. Below: hairline rows for active provider, model, endpoint, health (with latency + "last check" timestamp), refine prompt + summary schema "view default →" links.
- **Discord**: status banner (green dot + "Bot is online · 2 guilds" + "JDA 5.4 · sidecar · gateway latency 18 ms" in mono). Then hairline rows: bot token (masked + rotate), default guild, default channel, audio sink, auto-rejoin backoff. Then **Quick-connect presets** subsection: hairline rows of preset name + IDs in mono + "Set default" button + "remove" link.
- **Storage**: hairline rows of file system paths in mono. Then "Currently using" 4-column hairline grid: Recordings / Transcripts / Profiles / Models — kicker + serif numeral + mono caption.
- **About**: logo (72px, 12px radius) + serif italic "wisper" 28px + version line + tagline. Below: 2-col grid of Components (library + version) and Links (clickable serif italic items).

---

## 7. Design tokens for code

Suggested `tailwind.config.js` extension:

```js
module.exports = {
  theme: {
    extend: {
      colors: {
        ink: {
          900: '#080b12',  // bgSunken
          800: '#0b0f17',  // bg
          700: '#11161f',  // bgRaised
          600: '#161c26',  // bgRaised2
        },
        paper: {
          DEFAULT: '#f3ead8',  // text
          bright:  '#fff8e8',
          dim:     '#a3a89e',  // textDim
          faint:   '#5f6571',  // textFaint
        },
        rule:        'rgba(243, 234, 216, 0.09)',
        'rule-strong': 'rgba(243, 234, 216, 0.18)',
        accent: {
          DEFAULT: '#5fd4e7',  // cyan
          deep:    '#2a8da0',
        },
        signal: {
          green: '#7bd88f',
          amber: '#e4b572',
          rose:  '#e88b8b',
        },
      },
      fontFamily: {
        serif: ['Newsreader', 'Georgia', 'serif'],
        sans:  ['Geist', 'system-ui', 'sans-serif'],
        mono:  ['JetBrains Mono', 'ui-monospace', 'monospace'],
      },
      fontSize: {
        kicker: ['10px', { letterSpacing: '0.15em' }],
        kicker2: ['10px', { letterSpacing: '0.18em' }],
      },
    },
  },
};
```

And the Google Fonts URL to add to `base.html`:

```html
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=Geist:wght@300;400;500;600;700&family=Newsreader:ital,opsz,wght@0,6..72,300;0,6..72,400;0,6..72,500;1,6..72,400;1,6..72,500&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet"/>
```

---

## 8. Implementation plan

A suggested order:

1. **Tokens + base layout** — Replace `base.html`. Drop in Google Fonts link, switch to dark `bg`. Build the new `StudioSidebar` as a Jinja include `partials/sidebar.html`, and a `partials/toolbar.html` macro.
2. **Iconography** — The prototype defines a custom SVG icon set in `screens/codex.jsx` (search for `CodexIcon`). Copy those path definitions into an icon registry — either as Jinja macros, a small Web Component, or static SVGs in `static/icons/`. Don't pull in a generic icon library.
3. **Dashboard** (`index.html`) — Easiest screen. Establishes the hero pattern, stat strip, hairline tables, section heading conventions. Keep the htmx job polling already in place.
4. **Transcripts list + Transcript detail** — These are the heaviest read screens. The summary view is the marquee. Don't skip the drop cap or pull quote.
5. **Transcribe** — Mostly forms. Keep submission behavior identical; restyle the controls per the spec.
6. **Recordings list** — Status pill colors must match the spec — they communicate health.
7. **Record** (live screen) — Hardest. The per-speaker meters drive the design. SSE event names already exist in `record.py`; bind them to bar heights with a small vanilla JS module in `static/app.js`. Don't reach for a charting library — these are just 28 divs per speaker.
8. **Speakers, Campaigns, Config** — Lower priority but follow the same patterns. Config is the closest to a stock settings page; just keep the section-nav rhythm.

### What stays in htmx vs. needs new JS

- **htmx, as today**: dashboard `Recent Jobs` poll, transcribe stage updates, recordings list refresh, transcript refine status.
- **Vanilla JS, additions to `static/app.js`**:
  - `record-meters.js` — subscribes to `/record/sse`, updates each speaker row's bars + LIVE pill class based on `voice_activity` events.
  - `record-ticker.js` — same SSE stream, appends new lines to the ticker, fades older ones.
  - `config-section-nav.js` — switches the right pane (or use htmx `hx-get` + `hx-target`).

---

## 9. Files in this bundle

- `prototype-standalone.html` — single-file offline copy of the interactive prototype. Open this in a browser to navigate the design.
- `prototype.html` + `prototype.jsx` + `screens/*.jsx` + `assets/logo.png` — unbundled source files for the prototype. Read the JSX as reference for exact inline-style values when you need to look something up.
- `assets/logo.png` — the existing wisper logo. Already in the project at `src/wisper_transcribe/static/logo.png` — no replacement needed.

## 10. Source-of-truth for exact values

When in doubt about a specific value (color, padding, font-size), open the relevant `screens/*.jsx` file and search for the component. Every value used in the prototype is an inline-styled literal — no abstraction layer between you and the rendered design.

Key files by screen:

| Screen | File |
|---|---|
| Sidebar, toolbar, section heads, dashboard | `screens/studio.jsx` |
| Transcribe, transcript detail | `screens/studio2.jsx` |
| Record (live) | `screens/studio-record.jsx` |
| Recordings list, Speakers | `screens/studio-recordings.jsx` |
| Campaigns, Config | `screens/studio-campaigns.jsx` |
| Transcripts list | `screens/studio-transcripts-list.jsx` |
| Icon definitions | `screens/codex.jsx` (search `CodexIcon`) |
| Placeholder content data | `screens/shared-data.jsx` |

## 11. What was explored but rejected

The original conversation evaluated four design directions. Three were rejected in favor of Studio:

- **Codex** — Same editorial typography but no sidebar — chrome was a top-nav. Lost in density.
- **Aurora** — Wisp-glow backgrounds with glass cards. Beautiful but felt fragile for a tool you use during stressful live sessions.
- **Workshop** — The dense data-table version, but with Space Grotesk + JetBrains Mono throughout. Power-user energy without the literary mood.

Studio merges Workshop's structure with Codex's content typography. If you ever want to revisit the alternates, they're in the original project at `screens/codex.jsx`, `screens/aurora*.jsx`, `screens/workshop*.jsx`.

---

## 12. Open questions / decisions deferred

1. **Light mode.** Not designed yet — the brief asked for dark primary. If you add it later, invert the bg/text scale, keep cyan/amber/rose semantic colors identical.
2. **Audio player drawer.** "Listen" button shows on Transcript detail but the player isn't designed. Keep whatever exists, or come back to design it.
3. **Transcript inline-editing.** Mentioned as a likely next gap but not in this round. Inline speaker rename + word-level fix would belong on the Transcript tab (not Summary).
4. **Mobile.** Designs assume 1280+ desktop. The existing app has a mobile menu — preserve it as a fallback below 768px until a proper mobile pass is done.
