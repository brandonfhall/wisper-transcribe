// Codex — editorial dark. Newsreader serif italic display, hairline rules,
// paper-cream text on deep navy. Cyan reserved for active/live states only.

const codexTokens = {
  bg: '#0b1424',
  bgRaised: '#11192b',
  bgSunken: '#080f1d',
  rule: 'rgba(243, 234, 216, 0.10)',
  ruleStrong: 'rgba(243, 234, 216, 0.18)',
  text: '#f3ead8',
  textDim: '#a3aabb',
  textFaint: '#6b7488',
  accent: '#5fd4e7', // cyan, for active/live only
  accentDeep: '#2a8da0',
  amber: '#e4b572',  // for warnings / sanity tense
  rose: '#e88b8b',   // for danger
  serif: '"Newsreader", "Instrument Serif", Georgia, serif',
  sans: '"Geist", -apple-system, system-ui, sans-serif',
  mono: '"JetBrains Mono", ui-monospace, monospace',
};

const ct = codexTokens;

// --- Icon set (custom-feeling, slightly heavier strokes) ---
const CodexIcon = ({ name, size = 16, color = 'currentColor' }) => {
  const paths = {
    quill: <path d="M3 21l5-5M14 4l-9 9-1 5 5-1 9-9-4-4z M11 7l4 4" />,
    scroll: <path d="M5 4h11a3 3 0 013 3v10a3 3 0 01-3 3H8a3 3 0 01-3-3V4z M5 7H3 M16 4v13" />,
    waveform: <g><line x1="3" y1="12" x2="3" y2="12"/><line x1="6" y1="9" x2="6" y2="15"/><line x1="9" y1="5" x2="9" y2="19"/><line x1="12" y1="8" x2="12" y2="16"/><line x1="15" y1="3" x2="15" y2="21"/><line x1="18" y1="7" x2="18" y2="17"/><line x1="21" y1="10" x2="21" y2="14"/></g>,
    upload: <path d="M12 16V4 M6 10l6-6 6 6 M4 20h16" />,
    record: <circle cx="12" cy="12" r="6" fill={color} stroke="none"/>,
    spark: <path d="M12 3v6 M12 15v6 M3 12h6 M15 12h6 M5.6 5.6l4.2 4.2 M14.2 14.2l4.2 4.2 M5.6 18.4l4.2-4.2 M14.2 9.8l4.2-4.2" />,
    play: <path d="M6 4l14 8-14 8V4z" fill={color} stroke="none"/>,
    pause: <g><rect x="6" y="4" width="4" height="16" fill={color} stroke="none"/><rect x="14" y="4" width="4" height="16" fill={color} stroke="none"/></g>,
    chevron: <path d="M9 6l6 6-6 6"/>,
    chevronDown: <path d="M6 9l6 6 6-6"/>,
    dot: <circle cx="12" cy="12" r="3" fill={color} stroke="none"/>,
    dice: <g><path d="M12 2L3 7v10l9 5 9-5V7l-9-5z"/><path d="M3 7l9 5 9-5 M12 12v10"/></g>,
    user: <g><circle cx="12" cy="9" r="4"/><path d="M4 21c0-4 4-7 8-7s8 3 8 7"/></g>,
    users: <g><circle cx="9" cy="9" r="3.5"/><path d="M2 20c0-3.5 3-6 7-6s7 2.5 7 6"/><path d="M16 4a3.5 3.5 0 110 7 M22 18c0-2.5-1.5-4.5-4-5.5"/></g>,
    skull: <g><path d="M5 12a7 7 0 1114 0v3l1 3h-4l-1 2H9l-1-2H4l1-3v-3z"/><circle cx="9" cy="12" r="1.4" fill={color} stroke="none"/><circle cx="15" cy="12" r="1.4" fill={color} stroke="none"/><path d="M11 17h2"/></g>,
    book: <path d="M4 5a2 2 0 012-2h12v18H6a2 2 0 01-2-2V5z M8 3v18 M12 8h4 M12 12h4"/>,
    flame: <path d="M12 21c-4 0-7-3-7-7 0-3 2-5 3-7 1 2 2 3 4 3 0-4-1-6-1-7 4 2 8 6 8 11 0 4-3 7-7 7z"/>,
    coin: <g><circle cx="12" cy="12" r="8"/><path d="M9 9c0-1 1.5-2 3-2s3 1 3 2-1.5 1.5-3 2-3 1-3 2 1.5 2 3 2 3-1 3-2"/></g>,
    eye: <g><path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></g>,
    sliders: <g><line x1="4" y1="6" x2="20" y2="6"/><line x1="4" y1="12" x2="20" y2="12"/><line x1="4" y1="18" x2="20" y2="18"/><circle cx="8" cy="6" r="2" fill={ct.bg}/><circle cx="14" cy="12" r="2" fill={ct.bg}/><circle cx="9" cy="18" r="2" fill={ct.bg}/></g>,
    search: <g><circle cx="11" cy="11" r="6"/><path d="M20 20l-4-4"/></g>,
    settings: <g><circle cx="12" cy="12" r="3"/><path d="M19 12a7 7 0 00-.2-1.6l2.1-1.6-2-3.4-2.5.9a7 7 0 00-2.8-1.6L13 2h-4l-.6 2.7A7 7 0 005.6 6.3L3.1 5.4l-2 3.4 2.1 1.6A7 7 0 003 12c0 .5.1 1 .2 1.6l-2.1 1.6 2 3.4 2.5-.9a7 7 0 002.8 1.6L9 22h4l.6-2.7a7 7 0 002.8-1.6l2.5.9 2-3.4-2.1-1.6c.1-.6.2-1.1.2-1.6z"/></g>,
    plus: <path d="M12 5v14 M5 12h14"/>,
    arrowRight: <path d="M5 12h14 M13 6l6 6-6 6"/>,
  };
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
      {paths[name]}
    </svg>
  );
};

// --- Chrome (top bar) ---
const CodexNav = ({ active }) => {
  const items = ['Dashboard', 'Transcribe', 'Transcripts', 'Record', 'Speakers', 'Campaigns'];
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '18px 32px', borderBottom: `1px solid ${ct.rule}`, background: ct.bg }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
        <img src="assets/logo.png" width="34" height="34" style={{ borderRadius: 8, boxShadow: `0 0 0 1px ${ct.rule}` }}/>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
          <span style={{ fontFamily: ct.serif, fontStyle: 'italic', fontSize: 22, fontWeight: 400, color: ct.text, letterSpacing: '-0.01em' }}>wisper</span>
          <span style={{ fontFamily: ct.mono, fontSize: 10, color: ct.textFaint, letterSpacing: '0.08em' }}>v0.7.2</span>
        </div>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
        {items.map((label) => (
          <a key={label} style={{
            padding: '8px 14px', fontSize: 13, color: active === label ? ct.text : ct.textDim,
            fontWeight: active === label ? 500 : 400, position: 'relative',
            borderBottom: active === label ? `1px solid ${ct.text}` : '1px solid transparent',
            marginBottom: -1,
          }}>{label}</a>
        ))}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
        <div style={{ fontFamily: ct.mono, fontSize: 11, color: ct.textFaint, display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{ width: 6, height: 6, borderRadius: '50%', background: ct.accent, boxShadow: `0 0 8px ${ct.accent}` }}/>
          CUDA · 4070
        </div>
        <CodexIcon name="settings" size={16} color={ct.textDim}/>
        <div style={{ width: 30, height: 30, borderRadius: '50%', background: ct.bgRaised, color: ct.text, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 12, fontWeight: 500, border: `1px solid ${ct.rule}` }}>BH</div>
      </div>
    </div>
  );
};

const SectionLabel = ({ kicker, children, action }) => (
  <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', marginBottom: 16 }}>
    <div>
      {kicker && <div style={{ fontFamily: ct.mono, fontSize: 10, color: ct.textFaint, letterSpacing: '0.15em', textTransform: 'uppercase', marginBottom: 4 }}>{kicker}</div>}
      <h2 style={{ fontFamily: ct.serif, fontStyle: 'italic', fontWeight: 400, fontSize: 24, color: ct.text, margin: 0, letterSpacing: '-0.01em' }}>{children}</h2>
    </div>
    {action}
  </div>
);

// --- Screen 1: Dashboard ---
const CodexDashboard = () => {
  const d = window.WISPER_DATA;
  return (
    <div className="codex" data-screen-label="Codex · Dashboard" style={{ width: '100%', height: '100%', background: ct.bg, color: ct.text, fontFamily: ct.sans, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
      <CodexNav active="Dashboard"/>

      <div style={{ flex: 1, padding: '36px 32px 32px', overflow: 'auto' }}>
        {/* Editorial header */}
        <div style={{ display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between', marginBottom: 36, paddingBottom: 24, borderBottom: `1px solid ${ct.rule}` }}>
          <div>
            <div style={{ fontFamily: ct.mono, fontSize: 10, color: ct.textFaint, letterSpacing: '0.18em', textTransform: 'uppercase', marginBottom: 10 }}>Tuesday · April 28, 2026 · 9:42 PM</div>
            <h1 style={{ fontFamily: ct.serif, fontWeight: 300, fontSize: 56, color: ct.text, margin: 0, lineHeight: 1, letterSpacing: '-0.025em' }}>
              Good evening, <span style={{ fontStyle: 'italic', fontWeight: 400 }}>Brandon</span>.
            </h1>
            <p style={{ fontFamily: ct.serif, fontSize: 17, fontStyle: 'italic', color: ct.textDim, marginTop: 14, marginBottom: 0, maxWidth: 560 }}>
              Two sessions are processing. Theo is still missing.
            </p>
          </div>
          <div style={{ display: 'flex', gap: 10 }}>
            <button style={{ background: 'transparent', color: ct.text, border: `1px solid ${ct.ruleStrong}`, padding: '10px 16px', fontSize: 13, fontFamily: ct.sans, display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}>
              <CodexIcon name="upload" size={14}/> New transcription
            </button>
            <button style={{ background: ct.accent, color: ct.bg, border: 'none', padding: '10px 16px', fontSize: 13, fontWeight: 500, fontFamily: ct.sans, display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', boxShadow: `0 0 24px ${ct.accent}40` }}>
              <CodexIcon name="record" size={10} color={ct.bg}/> Start session
            </button>
          </div>
        </div>

        {/* Stat strip */}
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 0, borderTop: `1px solid ${ct.rule}`, borderBottom: `1px solid ${ct.rule}`, marginBottom: 32 }}>
          {[
            { kicker: 'In progress', value: '2', sub: 'jobs · 1 queued' },
            { kicker: 'Archive',     value: '38', sub: 'transcripts · 142h' },
            { kicker: 'Voices',      value: '6',  sub: 'enrolled speakers' },
            { kicker: 'Words',       value: '1.18M', sub: 'transcribed total' },
          ].map((s, i) => (
            <div key={i} style={{ padding: '22px 24px', borderLeft: i ? `1px solid ${ct.rule}` : 'none' }}>
              <div style={{ fontFamily: ct.mono, fontSize: 10, color: ct.textFaint, letterSpacing: '0.18em', textTransform: 'uppercase', marginBottom: 8 }}>{s.kicker}</div>
              <div style={{ fontFamily: ct.serif, fontSize: 36, fontWeight: 300, color: ct.text, lineHeight: 1 }}>{s.value}</div>
              <div style={{ fontSize: 12, color: ct.textDim, marginTop: 6 }}>{s.sub}</div>
            </div>
          ))}
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: '1.6fr 1fr', gap: 40 }}>
          {/* Active jobs */}
          <div>
            <SectionLabel kicker="Now processing"
              action={<a style={{ fontSize: 12, color: ct.textDim, fontFamily: ct.mono }}>view queue →</a>}
            >The desk</SectionLabel>

            {d.jobs.map((job, i) => (
              <div key={job.id} style={{ padding: '18px 0', borderTop: i === 0 ? `1px solid ${ct.rule}` : 'none', borderBottom: `1px solid ${ct.rule}` }}>
                <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', marginBottom: 10 }}>
                  <div style={{ display: 'flex', alignItems: 'baseline', gap: 12 }}>
                    {job.progress > 0 && job.progress < 1 ?
                      <span style={{ width: 8, height: 8, borderRadius: '50%', background: ct.accent, boxShadow: `0 0 10px ${ct.accent}`, marginRight: 2 }}/> :
                      <span style={{ width: 8, height: 8, borderRadius: '50%', background: 'transparent', border: `1px solid ${ct.textFaint}`, marginRight: 2 }}/>
                    }
                    <div style={{ fontSize: 14, fontWeight: 500, color: ct.text }}>{job.title}</div>
                  </div>
                  <div style={{ fontFamily: ct.mono, fontSize: 11, color: ct.textFaint }}>{job.startedAt}</div>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 14, marginLeft: 20 }}>
                  <div style={{ flex: 1, height: 2, background: ct.bgSunken, borderRadius: 1, overflow: 'hidden' }}>
                    <div style={{ width: `${job.progress * 100}%`, height: '100%', background: job.progress > 0 ? ct.accent : 'transparent', boxShadow: job.progress > 0 ? `0 0 6px ${ct.accent}80` : 'none' }}/>
                  </div>
                  <div style={{ fontFamily: ct.mono, fontSize: 11, color: ct.textDim, minWidth: 70 }}>{job.stage}</div>
                  <div style={{ fontFamily: ct.mono, fontSize: 11, color: ct.textFaint, minWidth: 50, textAlign: 'right' }}>{job.eta}</div>
                </div>
              </div>
            ))}

            <SectionLabel kicker="Recent" action={<a style={{ fontSize: 12, color: ct.textDim, fontFamily: ct.mono }}>all 38 transcripts →</a>}>The archive</SectionLabel>
            <div>
              {d.completed.slice(0, 4).map((t, i) => {
                const camp = d.campaigns.find(c => c.slug === t.campaign);
                return (
                  <div key={i} style={{ display: 'grid', gridTemplateColumns: '1fr auto auto auto', alignItems: 'baseline', padding: '14px 0', borderTop: `1px solid ${ct.rule}`, gap: 18 }}>
                    <div>
                      <div style={{ fontFamily: ct.serif, fontSize: 16, color: ct.text, fontWeight: 400 }}>{t.name}</div>
                      <div style={{ fontSize: 11, color: ct.textFaint, marginTop: 3, display: 'flex', alignItems: 'center', gap: 6 }}>
                        <span style={{ width: 6, height: 6, borderRadius: '50%', background: camp?.color }}/>
                        {camp?.name}
                      </div>
                    </div>
                    <div style={{ fontFamily: ct.mono, fontSize: 11, color: ct.textDim }}>{t.duration}</div>
                    <div style={{ fontFamily: ct.mono, fontSize: 11, color: ct.textFaint, minWidth: 60, textAlign: 'right' }}>{t.words.toLocaleString()} words</div>
                    <div style={{ fontFamily: ct.mono, fontSize: 11, color: ct.textFaint, minWidth: 50, textAlign: 'right' }}>{t.date}</div>
                  </div>
                );
              })}
            </div>
          </div>

          {/* Right column */}
          <div>
            <SectionLabel kicker="Campaigns">The tables</SectionLabel>
            <div style={{ borderTop: `1px solid ${ct.rule}` }}>
              {d.campaigns.map((c, i) => (
                <div key={c.slug} style={{ padding: '16px 0', borderBottom: `1px solid ${ct.rule}`, display: 'flex', alignItems: 'center', gap: 14 }}>
                  <div style={{ width: 2, height: 36, background: c.color, borderRadius: 1 }}/>
                  <div style={{ flex: 1 }}>
                    <div style={{ fontFamily: ct.serif, fontSize: 16, color: ct.text }}>{c.name}</div>
                    <div style={{ fontSize: 11, color: ct.textFaint, marginTop: 2, fontFamily: ct.mono, letterSpacing: '0.05em' }}>{c.system.toUpperCase()}</div>
                  </div>
                  <div style={{ textAlign: 'right' }}>
                    <div style={{ fontFamily: ct.serif, fontSize: 20, color: ct.text, fontWeight: 300 }}>{c.sessions}</div>
                    <div style={{ fontSize: 10, color: ct.textFaint, fontFamily: ct.mono, letterSpacing: '0.1em' }}>SESSIONS</div>
                  </div>
                </div>
              ))}
            </div>

            <div style={{ marginTop: 36 }}>
              <SectionLabel kicker="System">Studio</SectionLabel>
              <div style={{ borderTop: `1px solid ${ct.rule}` }}>
                {[
                  { k: 'Device',      v: 'CUDA · RTX 4070' },
                  { k: 'Model',       v: 'large-v3-turbo' },
                  { k: 'HF token',    v: '✓ configured', ok: true },
                  { k: 'Discord bot', v: '◌ idle' },
                  { k: 'LLM',         v: 'Ollama · llama3.1:70b' },
                ].map((row, i) => (
                  <div key={i} style={{ display: 'flex', justifyContent: 'space-between', padding: '11px 0', borderBottom: `1px solid ${ct.rule}` }}>
                    <span style={{ fontSize: 12, color: ct.textDim }}>{row.k}</span>
                    <span style={{ fontFamily: ct.mono, fontSize: 11, color: row.ok ? ct.accent : ct.text }}>{row.v}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

window.CodexDashboard = CodexDashboard;
window.CodexNav = CodexNav;
window.CodexIcon = CodexIcon;
window.codexTokens = codexTokens;
window.CodexSectionLabel = SectionLabel;
