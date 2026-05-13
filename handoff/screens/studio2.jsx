// Studio — Transcribe + Transcript Detail.

const st2 = window.studioTokens;
const SICN = window.SIcon;
const SSidebar = window.StudioSidebar;
const SToolbar = window.StudioToolbar;
const SSH = window.StudioSectionHead;

// --- Transcribe ---
const StudioTranscribe = ({ onNavigate }) => {
  const d = window.WISPER_DATA;
  return (
    <div data-screen-label="Studio · Transcribe" style={{
      width: '100%', height: '100%', background: st2.bg, color: st2.text,
      fontFamily: st2.sans, overflow: 'hidden', display: 'flex',
    }}>
      <SSidebar active="Transcribe" onNavigate={onNavigate}/>
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        <SToolbar kicker="Transcribe" title="New entry" sub="/transcribe · NEW JOB"
          actions={
            <button style={{ background: st2.cyan, color: st2.bg, border: 'none', padding: '7px 16px', borderRadius: 6, fontSize: 13, fontFamily: st2.sans, fontWeight: 600, cursor: 'pointer' }}>
              Run job ↵
            </button>
          }
        />

        <div style={{ flex: 1, padding: '24px 28px', overflow: 'auto', display: 'grid', gridTemplateColumns: '1.55fr 1fr', gap: 32, alignContent: 'start' }}>
          {/* Left */}
          <div>
            {/* Drop zone — Codex-style with serif italic */}
            <SSH kicker="Source">Add a session</SSH>
            <div style={{
              border: `1px dashed ${st2.ruleStrong}`,
              padding: '52px 32px', textAlign: 'center',
              background: `linear-gradient(180deg, transparent 0%, ${st2.cyan}06 100%)`,
            }}>
              <div style={{ display: 'flex', justifyContent: 'center', marginBottom: 16 }}>
                <div style={{ width: 52, height: 52, borderRadius: '50%', border: `1px solid ${st2.ruleStrong}`, display: 'flex', alignItems: 'center', justifyContent: 'center', color: st2.textDim }}>
                  <SICN name="waveform" size={26} color={st2.textDim}/>
                </div>
              </div>
              <div style={{ fontFamily: st2.serif, fontStyle: 'italic', fontSize: 22, color: st2.text }}>Drop audio here.</div>
              <div style={{ fontSize: 13, color: st2.textDim, marginTop: 8 }}>
                Or <span style={{ color: st2.cyan, textDecoration: 'underline', textUnderlineOffset: 3 }}>browse files</span> &nbsp;·&nbsp; paste a path &nbsp;·&nbsp; pick a Discord recording
              </div>
              <div style={{ fontFamily: st2.mono, fontSize: 10, color: st2.textFaint, marginTop: 16, letterSpacing: '0.12em' }}>
                MP3 · WAV · M4A · MP4 · MKV · OGG · ≤ 8h
              </div>
            </div>

            {/* Selected file row */}
            <div style={{ marginTop: 12, display: 'grid', gridTemplateColumns: '14px 1fr auto auto auto', gap: 14, alignItems: 'center', padding: '12px 14px', background: st2.bgRaised, border: `1px solid ${st2.rule}`, borderRadius: 6 }}>
              <SICN name="waveform" size={14} color={st2.cyan}/>
              <div>
                <div style={{ fontFamily: st2.serif, fontSize: 15, color: st2.text }}>S1 E7 — The Yellow Throne</div>
                <div style={{ fontFamily: st2.mono, fontSize: 10.5, color: st2.textFaint, marginTop: 3 }}>~/sessions/impossible/E7.mp3</div>
              </div>
              <span style={{ fontFamily: st2.mono, fontSize: 11, color: st2.textDim }}>487.2 MB</span>
              <span style={{ fontFamily: st2.mono, fontSize: 11, color: st2.textDim }}>04:08:00</span>
              <span style={{ fontFamily: st2.mono, fontSize: 11, color: st2.textFaint, cursor: 'pointer' }}>✕</span>
            </div>

            {/* Live job */}
            <div style={{ marginTop: 36 }}>
              <SSH kicker="Processing now · Job j_8a1f" action={<a style={{ fontFamily: st2.mono, fontSize: 11, color: st2.textDim }}>cancel ✕</a>}>
                S1 E7 — <span style={{ fontFamily: st2.serif }}>The Yellow Throne</span>
              </SSH>

              {/* Pipeline strip */}
              <div style={{ display: 'flex', borderTop: `1px solid ${st2.rule}`, borderBottom: `1px solid ${st2.rule}`, marginBottom: 0 }}>
                {[
                  { n: 'Convert',    s: 'done', d: '4.2s' },
                  { n: 'Transcribe', s: 'done', d: '12m' },
                  { n: 'Diarize',    s: 'active', d: '62%' },
                  { n: 'Align',      s: 'pending', d: '—' },
                  { n: 'Identify',   s: 'pending', d: '—' },
                  { n: 'Refine',     s: 'pending', d: '—' },
                ].map((stage, i) => (
                  <div key={i} style={{
                    flex: 1, padding: '12px 14px',
                    borderLeft: i ? `1px solid ${st2.rule}` : 'none',
                    background: stage.s === 'active' ? `${st2.cyan}08` : 'transparent',
                  }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 7, marginBottom: 5 }}>
                      {stage.s === 'done' &&    <span style={{ width: 4, height: 4, borderRadius: '50%', background: st2.green }}/>}
                      {stage.s === 'active' &&  <span style={{ width: 6, height: 6, borderRadius: '50%', background: st2.cyan, boxShadow: `0 0 6px ${st2.cyan}` }}/>}
                      {stage.s === 'pending' && <span style={{ width: 4, height: 4, borderRadius: '50%', border: `1px solid ${st2.textFaint}` }}/>}
                      <div style={{ fontFamily: st2.mono, fontSize: 10, color: stage.s === 'active' ? st2.cyan : stage.s === 'done' ? st2.green : st2.textFaint, letterSpacing: '0.12em', textTransform: 'uppercase' }}>{stage.n}</div>
                    </div>
                    <div style={{ fontFamily: stage.s === 'active' ? st2.serif : st2.mono, fontSize: stage.s === 'active' ? 17 : 11, color: stage.s === 'active' ? st2.text : st2.textDim, fontWeight: stage.s === 'active' ? 400 : 400, fontStyle: stage.s === 'active' ? 'normal' : 'normal' }}>{stage.d}</div>
                  </div>
                ))}
              </div>

              {/* Overall progress */}
              <div style={{ padding: '14px 0', borderBottom: `1px solid ${st2.rule}`, display: 'flex', alignItems: 'center', gap: 14 }}>
                <div style={{ flex: 1, height: 3, background: st2.bgSunken, borderRadius: 2, overflow: 'hidden' }}>
                  <div style={{ width: '38%', height: '100%', background: st2.cyan, boxShadow: `0 0 6px ${st2.cyan}80` }}/>
                </div>
                <div style={{ fontFamily: st2.mono, fontSize: 12, color: st2.text, minWidth: 96, textAlign: 'right' }}>38% · ~4 min</div>
              </div>

              {/* Log */}
              <div style={{ background: st2.bgSunken, padding: '14px 16px', fontFamily: st2.mono, fontSize: 11, color: st2.textDim, lineHeight: 1.8, borderBottom: `1px solid ${st2.rule}`, maxHeight: 180, overflow: 'hidden' }}>
                {[
                  { t: '21:38:12', tag: 'convert',  c: st2.textFaint, msg: '16 kHz mono WAV ready (487.2 MB)' },
                  { t: '21:38:16', tag: 'whisper',  c: st2.textFaint, msg: 'large-v3-turbo loaded on cuda:0 (float16, beam=5)' },
                  { t: '21:38:18', tag: 'whisper',  c: st2.textFaint, msg: 'hotwords: Macallistar Carcosa Hastur "Abigail Wright" "King in Yellow"' },
                  { t: '21:50:02', tag: 'whisper',  c: st2.green,     msg: '14,127 segments · 30,891 words · 11m 44s' },
                  { t: '21:50:09', tag: 'diarize',  c: st2.cyan,      msg: 'pyannote 3.1 · scanning for 5 speakers' },
                  { t: '21:51:38', tag: 'diarize',  c: st2.cyan,      msg: '61% · seg=4127 spk=5 · 02:33:09 / 04:08:00' },
                  { t: '21:51:42', tag: 'diarize',  c: st2.cyan,      msg: 'emb_extract speaker_03 (0.84 sim)' },
                  { t: '21:51:44', tag: 'diarize',  c: st2.cyan,      msg: '62% · 02:34:11 / 04:08:00' },
                ].map((l, i) => (
                  <div key={i} style={{ display: 'flex', gap: 12 }}>
                    <span style={{ color: st2.textFaint, minWidth: 64 }}>{l.t}</span>
                    <span style={{ color: l.c, minWidth: 64 }}>{l.tag}</span>
                    <span style={{ flex: 1 }}>{l.msg}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>

          {/* Settings — Codex hairlines + Workshop CLI flags */}
          <div style={{ alignSelf: 'start' }}>
            <SSH kicker="Run with">Settings</SSH>

            <div style={{ borderTop: `1px solid ${st2.rule}` }}>
              {/* Campaign */}
              <div style={{ padding: '16px 0', borderBottom: `1px solid ${st2.rule}` }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 10 }}>
                  <div style={{ fontFamily: st2.mono, fontSize: 10, color: st2.textFaint, letterSpacing: '0.15em', textTransform: 'uppercase' }}>Campaign</div>
                  <div style={{ fontFamily: st2.mono, fontSize: 10, color: st2.textFaint }}>--campaign</div>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                    <span style={{ width: 8, height: 8, borderRadius: '50%', background: d.campaigns[0].color }}/>
                    <span style={{ fontFamily: st2.serif, fontSize: 18, color: st2.text }}>{d.campaigns[0].name}</span>
                  </div>
                  <SICN name="chevronDown" size={14} color={st2.textDim}/>
                </div>
                <div style={{ fontSize: 11.5, color: st2.textFaint, marginTop: 6 }}>{d.campaigns[0].system} · 7 prior sessions</div>
              </div>

              {/* Model */}
              <div style={{ padding: '16px 0', borderBottom: `1px solid ${st2.rule}` }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 10 }}>
                  <div style={{ fontFamily: st2.mono, fontSize: 10, color: st2.textFaint, letterSpacing: '0.15em', textTransform: 'uppercase' }}>Whisper model</div>
                  <div style={{ fontFamily: st2.mono, fontSize: 10, color: st2.textFaint }}>--model</div>
                </div>
                <div style={{ display: 'flex', gap: 0, border: `1px solid ${st2.rule}` }}>
                  {['small', 'medium', 'large-v3-turbo'].map((m, i) => (
                    <div key={m} style={{
                      flex: 1, padding: '8px 10px', textAlign: 'center',
                      fontSize: 11.5, fontFamily: st2.mono,
                      borderLeft: i ? `1px solid ${st2.rule}` : 'none',
                      background: m === 'large-v3-turbo' ? st2.bgRaised : 'transparent',
                      color: m === 'large-v3-turbo' ? st2.cyan : st2.textDim,
                      cursor: 'pointer',
                    }}>{m}</div>
                  ))}
                </div>
              </div>

              {/* Speakers */}
              <div style={{ padding: '16px 0', borderBottom: `1px solid ${st2.rule}` }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 12 }}>
                  <div style={{ fontFamily: st2.mono, fontSize: 10, color: st2.textFaint, letterSpacing: '0.15em', textTransform: 'uppercase' }}>Speakers</div>
                  <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
                    <span style={{ fontFamily: st2.serif, fontSize: 22, color: st2.cyan, lineHeight: 1 }}>5</span>
                    <span style={{ fontFamily: st2.mono, fontSize: 10, color: st2.textFaint }}>--num-speakers</span>
                  </div>
                </div>
                <div style={{ display: 'flex', gap: 3 }}>
                  {[1,2,3,4,5,6,7,8,9,10].map((n) => (
                    <div key={n} style={{
                      flex: 1, height: 24,
                      background: n <= 5 ? st2.bgRaised : st2.bgSunken,
                      borderTop: n === 5 ? `1px solid ${st2.cyan}` : 'none',
                      fontFamily: st2.mono, fontSize: 10,
                      color: n === 5 ? st2.cyan : st2.textFaint,
                      display: 'flex', alignItems: 'center', justifyContent: 'center',
                    }}>{n}</div>
                  ))}
                </div>
              </div>

              {/* Post-processing */}
              <div style={{ padding: '16px 0', borderBottom: `1px solid ${st2.rule}` }}>
                <div style={{ fontFamily: st2.mono, fontSize: 10, color: st2.textFaint, letterSpacing: '0.15em', textTransform: 'uppercase', marginBottom: 12 }}>After transcription</div>
                {[
                  { label: 'Refine',                  flag: '--refine',     sub: 'Fix vocab + speaker ID via LLM',  on: true },
                  { label: 'Summarize',               flag: '--summarize',  sub: 'Recap, loot, NPCs, follow-ups',    on: true },
                  { label: 'Update voice profiles',   flag: '--enroll-update', sub: 'EMA blend new audio in',        on: false },
                ].map((p) => (
                  <div key={p.label} style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', padding: '9px 0', gap: 12 }}>
                    <div>
                      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
                        <div style={{ fontFamily: st2.serif, fontSize: 14, color: st2.text }}>{p.label}</div>
                        <div style={{ fontFamily: st2.mono, fontSize: 10, color: st2.textFaint }}>{p.flag}</div>
                      </div>
                      <div style={{ fontSize: 11.5, color: st2.textFaint, marginTop: 2 }}>{p.sub}</div>
                    </div>
                    <div style={{
                      width: 28, height: 16, borderRadius: 8, padding: 2,
                      background: p.on ? st2.cyan : 'transparent',
                      border: p.on ? 'none' : `1px solid ${st2.ruleStrong}`,
                      display: 'flex', justifyContent: p.on ? 'flex-end' : 'flex-start', alignItems: 'center',
                      boxShadow: p.on ? `0 0 12px ${st2.cyan}50` : 'none', flexShrink: 0,
                    }}>
                      <div style={{ width: 12, height: 12, borderRadius: '50%', background: p.on ? st2.bg : st2.textFaint }}/>
                    </div>
                  </div>
                ))}
              </div>

              {/* Vocab */}
              <div style={{ padding: '16px 0', borderBottom: `1px solid ${st2.rule}` }}>
                <div style={{ fontFamily: st2.mono, fontSize: 10, color: st2.textFaint, letterSpacing: '0.15em', textTransform: 'uppercase', marginBottom: 10 }}>Vocabulary hints</div>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                  {['Macallistar', 'Carcosa', 'Hastur', 'Abigail Wright', 'King in Yellow', '+ add'].map((tag, i) => (
                    <span key={tag} style={{
                      fontSize: 11.5, padding: '4px 9px',
                      border: `1px solid ${st2.rule}`,
                      background: i === 5 ? 'transparent' : st2.bgRaised,
                      color: i === 5 ? st2.textDim : st2.text,
                      fontFamily: i === 5 ? st2.sans : st2.serif,
                      fontStyle: i === 5 ? 'normal' : 'italic',
                    }}>{tag}</span>
                  ))}
                </div>
              </div>

              {/* CLI preview */}
              <div style={{ padding: '16px 0' }}>
                <div style={{ fontFamily: st2.mono, fontSize: 10, color: st2.textFaint, letterSpacing: '0.15em', textTransform: 'uppercase', marginBottom: 10 }}>Equivalent CLI</div>
                <div style={{ padding: '10px 12px', background: st2.bgSunken, border: `1px solid ${st2.rule}`, fontFamily: st2.mono, fontSize: 11, color: st2.text, lineHeight: 1.7, wordBreak: 'break-all' }}>
                  <span style={{ color: st2.cyan }}>wisper</span> transcribe <span style={{ color: st2.green }}>E7.mp3</span>{' '}--campaign impossible-landscapes --num-speakers 5 --refine --summarize
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

// --- Transcript Detail ---
const StudioTranscript = ({ onNavigate }) => {
  const t = window.WISPER_DATA.transcript;
  const d = window.WISPER_DATA;
  return (
    <div data-screen-label="Studio · Transcript" style={{
      width: '100%', height: '100%', background: st2.bg, color: st2.text,
      fontFamily: st2.sans, overflow: 'hidden', display: 'flex',
    }}>
      <SSidebar active="Transcripts" onNavigate={onNavigate}/>
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        {/* Breadcrumb toolbar */}
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '14px 24px', borderBottom: `1px solid ${st2.rule}`, height: 60, flexShrink: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, fontSize: 12, color: st2.textDim, fontFamily: st2.mono, letterSpacing: '0.04em' }}>
            <a onClick={() => onNavigate && onNavigate('Transcripts')} style={{ cursor: 'pointer' }}>transcripts</a>
            <span style={{ color: st2.textFaint }}>/</span>
            <a onClick={() => onNavigate && onNavigate('Campaigns')} style={{ cursor: 'pointer' }}>impossible-landscapes</a>
            <span style={{ color: st2.textFaint }}>/</span>
            <span style={{ color: st2.text }}>S1E3-kings-and-queens.md</span>
          </div>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <span style={{ fontFamily: st2.mono, fontSize: 10.5, color: st2.green, letterSpacing: '0.12em', marginRight: 6 }}>● REFINED 22:18</span>
            <button style={{ background: st2.bgRaised, border: `1px solid ${st2.rule}`, color: st2.text, padding: '6px 12px', borderRadius: 5, fontSize: 12, fontFamily: st2.sans, display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer' }}>
              <SICN name="play" size={11}/> Listen
            </button>
            <button style={{ background: st2.bgRaised, border: `1px solid ${st2.rule}`, color: st2.text, padding: '6px 12px', borderRadius: 5, fontSize: 12, fontFamily: st2.sans, cursor: 'pointer' }}>Export .md</button>
            <button style={{ background: st2.bgRaised, border: `1px solid ${st2.rule}`, color: st2.text, padding: '6px 12px', borderRadius: 5, fontSize: 12, fontFamily: st2.sans, cursor: 'pointer' }}>Re-refine</button>
          </div>
        </div>

        <div style={{ flex: 1, overflow: 'auto' }}>
          {/* Hero — Codex serif italic title, Workshop meta strip below */}
          <div style={{ padding: '32px 36px 24px', borderBottom: `1px solid ${st2.rule}` }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, fontFamily: st2.mono, fontSize: 10, color: st2.textFaint, letterSpacing: '0.18em', textTransform: 'uppercase', marginBottom: 14 }}>
              <span style={{ width: 6, height: 6, borderRadius: '50%', background: '#7d4d8c' }}/>
              <span style={{ color: st2.textDim }}>{t.campaign}</span>
              <span>·</span>
              <span>{t.system}</span>
              <span>·</span>
              <span>{t.episode}</span>
            </div>
            <h1 style={{ fontFamily: st2.serif, fontWeight: 300, fontSize: 56, color: st2.text, margin: 0, lineHeight: 1, letterSpacing: '-0.03em' }}>
              Kings <span style={{ fontStyle: 'italic', fontWeight: 400 }}>and</span> Queens
            </h1>
            <div style={{ display: 'flex', gap: 28, marginTop: 18, fontFamily: st2.mono, fontSize: 11 }}>
              {[
                ['DATE',     'apr 14, 2026'],
                ['DURATION', t.duration],
                ['WORDS',    t.words.toLocaleString()],
                ['VOICES',   t.speakers],
                ['SANITY',   t.sanity, st2.amber],
              ].map(([k, v, c]) => (
                <div key={k}>
                  <div style={{ color: st2.textFaint, letterSpacing: '0.14em' }}>{k}</div>
                  <div style={{ color: c || st2.text, marginTop: 3 }}>{v}</div>
                </div>
              ))}
            </div>
          </div>

          {/* Tabs */}
          <div style={{ display: 'flex', borderBottom: `1px solid ${st2.rule}`, padding: '0 36px' }}>
            {[
              { name: 'Summary', active: true, icon: 'book', badge: 'DM notes' },
              { name: 'Transcript', active: false, icon: 'scroll' },
              { name: 'Speakers', active: false, icon: 'users' },
              { name: 'Audio', active: false, icon: 'waveform' },
              { name: 'Raw .md', active: false, icon: 'sliders' },
            ].map((tab) => (
              <div key={tab.name} style={{
                padding: '13px 14px', fontSize: 12,
                color: tab.active ? st2.text : st2.textDim,
                fontFamily: st2.mono, letterSpacing: '0.08em', textTransform: 'uppercase',
                borderBottom: tab.active ? `2px solid ${st2.cyan}` : '2px solid transparent',
                marginBottom: -1, display: 'flex', alignItems: 'center', gap: 7, cursor: 'pointer',
                fontWeight: 500,
              }}>
                <SICN name={tab.icon} size={11} color={tab.active ? st2.cyan : st2.textDim}/>
                {tab.name}
                {tab.badge && <span style={{ fontSize: 9, color: st2.cyan, padding: '2px 6px', border: `1px solid ${st2.cyan}40`, marginLeft: 4, letterSpacing: '0.08em' }}>{tab.badge}</span>}
              </div>
            ))}
          </div>

          {/* Body */}
          <div style={{ padding: '36px 36px 36px', display: 'grid', gridTemplateColumns: '1.55fr 1fr', gap: 48, alignItems: 'start' }}>
            {/* Main */}
            <div>
              {/* Recap — Codex editorial with drop cap */}
              <div style={{ marginBottom: 36 }}>
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 14, marginBottom: 16 }}>
                  <div style={{ fontFamily: st2.mono, fontSize: 10, color: st2.textFaint, letterSpacing: '0.2em', textTransform: 'uppercase' }}>I · Recap</div>
                  <div style={{ flex: 1, height: 1, background: st2.rule }}/>
                  <div style={{ fontFamily: st2.mono, fontSize: 10, color: st2.textFaint, letterSpacing: '0.12em' }}>3 PARAGRAPHS · 184 WORDS</div>
                </div>
                <div style={{ fontFamily: st2.serif, fontSize: 17, lineHeight: 1.65, color: st2.text }}>
                  {t.recap.map((para, i) => (
                    <p key={i} style={{ margin: '0 0 14px', textIndent: i === 0 ? 0 : '1.5em' }}>
                      {i === 0 && <span style={{ fontFamily: st2.serif, fontSize: 46, fontWeight: 400, color: st2.text, float: 'left', lineHeight: 0.9, marginRight: 8, marginTop: 6 }}>{para[0]}</span>}
                      {i === 0 ? para.slice(1) : para}
                    </p>
                  ))}
                </div>
              </div>

              {/* Pull quote */}
              <div style={{ borderTop: `1px solid ${st2.rule}`, borderBottom: `1px solid ${st2.rule}`, padding: '24px 0', margin: '36px 0' }}>
                <blockquote style={{ fontFamily: st2.serif, fontStyle: 'italic', fontSize: 24, fontWeight: 300, color: st2.text, margin: 0, lineHeight: 1.4, letterSpacing: '-0.01em' }}>
                  "He waved at me. He waved at me and no one else saw it."
                </blockquote>
                <div style={{ fontFamily: st2.mono, fontSize: 10.5, color: st2.textFaint, marginTop: 12, letterSpacing: '0.12em' }}>— YUKI · 02:14:22</div>
              </div>

              {/* Loot */}
              <div style={{ marginBottom: 36 }}>
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 14, marginBottom: 16 }}>
                  <div style={{ fontFamily: st2.mono, fontSize: 10, color: st2.textFaint, letterSpacing: '0.2em', textTransform: 'uppercase' }}>II · Loot &amp; objects</div>
                  <div style={{ flex: 1, height: 1, background: st2.rule }}/>
                  <div style={{ fontFamily: st2.mono, fontSize: 10, color: st2.textFaint, letterSpacing: '0.12em' }}>{t.loot.length} ITEMS · 3 UNUSUAL</div>
                </div>
                <div style={{ borderTop: `1px solid ${st2.rule}` }}>
                  {t.loot.map((item, i) => (
                    <div key={i} style={{ padding: '14px 0', borderBottom: `1px solid ${st2.rule}` }}>
                      <div style={{ display: 'grid', gridTemplateColumns: '14px 1fr 1.3fr 70px', gap: 16, alignItems: 'baseline' }}>
                        <div style={{ color: item.mundane ? st2.textFaint : st2.cyan, fontSize: 12 }}>{item.mundane ? '◦' : '◈'}</div>
                        <div style={{ fontFamily: st2.serif, fontSize: 16, color: st2.text }}>{item.name}</div>
                        <div style={{ fontSize: 12.5, color: st2.textDim }}>{item.where}</div>
                        <div style={{ textAlign: 'right' }}>
                          <span style={{ fontFamily: st2.mono, fontSize: 9.5, padding: '2px 7px', letterSpacing: '0.12em', color: item.mundane ? st2.textFaint : st2.cyan, border: `1px solid ${item.mundane ? st2.rule : st2.cyan + '50'}` }}>{item.mundane ? 'MUNDANE' : 'UNUSUAL'}</span>
                        </div>
                      </div>
                      {item.note && <div style={{ paddingLeft: 30, marginTop: 6, fontSize: 12.5, color: st2.textFaint, fontFamily: st2.serif, fontStyle: 'italic' }}>{item.note}</div>}
                    </div>
                  ))}
                </div>
              </div>

              {/* Excerpt */}
              <div>
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 14, marginBottom: 16 }}>
                  <div style={{ fontFamily: st2.mono, fontSize: 10, color: st2.textFaint, letterSpacing: '0.2em', textTransform: 'uppercase' }}>III · Pivotal moment</div>
                  <div style={{ flex: 1, height: 1, background: st2.rule }}/>
                  <div style={{ fontFamily: st2.mono, fontSize: 10, color: st2.textFaint, letterSpacing: '0.12em' }}>02:14:08 – 02:14:27</div>
                </div>
                <div style={{ background: st2.bgRaised, border: `1px solid ${st2.rule}`, padding: '16px 18px' }}>
                  {t.excerpt.map((line, i) => {
                    const sp = d.speakers.find(s => s.name === line.speaker);
                    return (
                      <div key={i} style={{ display: 'grid', gridTemplateColumns: '64px 90px 1fr', gap: 14, padding: '6px 0', alignItems: 'baseline' }}>
                        <span style={{ fontFamily: st2.mono, fontSize: 10.5, color: st2.textFaint }}>{line.t}</span>
                        <span style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
                          <span style={{ width: 6, height: 6, borderRadius: '50%', background: `oklch(0.6 0.14 ${sp?.hue || 0})` }}/>
                          <span style={{ fontFamily: st2.serif, fontSize: 13, color: st2.text }}>{line.speaker}</span>
                        </span>
                        <span style={{ fontSize: 13.5, color: st2.textDim, lineHeight: 1.55 }}>{line.text}</span>
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>

            {/* Sidebar */}
            <div>
              {/* Follow-ups */}
              <div style={{ background: st2.bgRaised, border: `1px solid ${st2.cyan}40`, padding: '20px 22px', marginBottom: 28, boxShadow: `0 0 28px ${st2.cyan}12` }}>
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginBottom: 14 }}>
                  <SICN name="spark" size={13} color={st2.cyan}/>
                  <div style={{ fontFamily: st2.mono, fontSize: 10, color: st2.cyan, letterSpacing: '0.18em', textTransform: 'uppercase' }}>Follow-ups</div>
                  <div style={{ marginLeft: 'auto', fontFamily: st2.mono, fontSize: 9.5, color: st2.textFaint, letterSpacing: '0.12em' }}>NEXT SESSION</div>
                </div>
                <ol style={{ margin: 0, padding: 0, listStyle: 'none' }}>
                  {t.followups.map((f, i) => (
                    <li key={i} style={{ display: 'flex', gap: 14, padding: '11px 0', borderTop: i ? `1px solid ${st2.rule}` : 'none' }}>
                      <span style={{ fontFamily: st2.serif, fontSize: 22, color: st2.textFaint, fontWeight: 300, lineHeight: 1, minWidth: 22 }}>{(i+1).toString().padStart(2, '0')}</span>
                      <span style={{ fontFamily: st2.serif, fontSize: 14.5, color: st2.text, lineHeight: 1.45, fontStyle: 'italic' }}>{f}</span>
                    </li>
                  ))}
                </ol>
              </div>

              {/* NPCs */}
              <div style={{ marginBottom: 28 }}>
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 14, marginBottom: 14 }}>
                  <div style={{ fontFamily: st2.mono, fontSize: 10, color: st2.textFaint, letterSpacing: '0.18em', textTransform: 'uppercase' }}>NPCs / persons</div>
                  <div style={{ flex: 1, height: 1, background: st2.rule }}/>
                </div>
                <div style={{ borderTop: `1px solid ${st2.rule}` }}>
                  {t.npcs.map((npc, i) => (
                    <div key={i} style={{ padding: '14px 0', borderBottom: `1px solid ${st2.rule}` }}>
                      <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', marginBottom: 4 }}>
                        <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
                          <div style={{ fontFamily: st2.serif, fontSize: 16, color: st2.text }}>{npc.name}</div>
                        </div>
                        <span style={{
                          fontFamily: st2.mono, fontSize: 9.5, letterSpacing: '0.12em', padding: '2px 7px',
                          background: npc.status === 'Missing' ? st2.rose + '14' : npc.status === 'Unknown' ? st2.amber + '14' : st2.green + '12',
                          color: npc.status === 'Missing' ? st2.rose : npc.status === 'Unknown' ? st2.amber : st2.green,
                          border: `1px solid ${npc.status === 'Missing' ? st2.rose + '40' : npc.status === 'Unknown' ? st2.amber + '40' : st2.green + '30'}`,
                        }}>{npc.status.toUpperCase()}</span>
                      </div>
                      <div style={{ fontFamily: st2.mono, fontSize: 10.5, color: st2.textFaint, letterSpacing: '0.1em', marginBottom: 5 }}>{npc.role.toUpperCase()}</div>
                      <div style={{ fontSize: 12.5, color: st2.textDim, fontFamily: st2.serif, fontStyle: 'italic', lineHeight: 1.45 }}>{npc.note}</div>
                    </div>
                  ))}
                </div>
              </div>

              {/* Voice distribution */}
              <div style={{ marginBottom: 24 }}>
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 14, marginBottom: 14 }}>
                  <div style={{ fontFamily: st2.mono, fontSize: 10, color: st2.textFaint, letterSpacing: '0.18em', textTransform: 'uppercase' }}>Voice distribution</div>
                  <div style={{ flex: 1, height: 1, background: st2.rule }}/>
                </div>
                <div>
                  {[24, 22, 31, 18, 12].map((pct, i) => {
                    const s = d.speakers[i];
                    return (
                      <div key={s.name} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '7px 0' }}>
                        <div style={{ width: 70, fontFamily: st2.serif, fontSize: 13, color: st2.text, display: 'flex', alignItems: 'center', gap: 7 }}>
                          <span style={{ width: 6, height: 6, borderRadius: '50%', background: `oklch(0.6 0.14 ${s.hue})` }}/>
                          {s.name}
                        </div>
                        <div style={{ flex: 1, height: 6, background: st2.bgSunken, overflow: 'hidden' }}>
                          <div style={{ width: `${pct}%`, height: '100%', background: `oklch(0.6 0.14 ${s.hue})` }}/>
                        </div>
                        <div style={{ fontFamily: st2.mono, fontSize: 11, color: st2.textDim, minWidth: 28, textAlign: 'right' }}>{pct}%</div>
                      </div>
                    );
                  })}
                </div>
              </div>

              {/* Refine receipt */}
              <div style={{ padding: '12px 14px', border: `1px solid ${st2.rule}`, background: st2.bgSunken, fontFamily: st2.mono, fontSize: 10.5, color: st2.textDim, lineHeight: 1.8 }}>
                <div style={{ color: st2.green, letterSpacing: '0.12em', marginBottom: 4 }}>✓ REFINE RECEIPT</div>
                <div>vocab: 14 corrections · Macallistar, Hastur, …</div>
                <div>speakers: 5/5 matched (avg 0.83 sim)</div>
                <div>llm: ollama llama3.1:70b · 1842 tokens</div>
                <div>completed: apr 14 22:18 (4m 02s)</div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

window.StudioTranscribe = StudioTranscribe;
window.StudioTranscript = StudioTranscript;
