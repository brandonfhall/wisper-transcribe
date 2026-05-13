// Studio · Campaigns + Config

const sx = window.studioTokens;
const SXIcon = window.SIcon;
const SXSidebar = window.StudioSidebar;
const SXSH = window.StudioSectionHead;
const SXToolbar = window.StudioToolbar;

// --- Campaigns (list + active detail) ---
const StudioCampaigns = ({ onNavigate }) => {
  const d = window.WISPER_DATA;
  const active = d.campaigns[0]; // Impossible Landscapes
  return (
    <div data-screen-label="Studio · Campaigns" style={{
      width: '100%', height: '100%', background: sx.bg, color: sx.text,
      fontFamily: sx.sans, overflow: 'hidden', display: 'flex',
    }}>
      <SXSidebar active="Campaigns" onNavigate={onNavigate}/>
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        <SXToolbar kicker="Campaigns" title={active.name} sub={`${active.system.toUpperCase()} · ${active.sessions} SESSIONS`}
          actions={
            <button style={{ background: sx.cyan, color: sx.bg, border: 'none', padding: '7px 14px', borderRadius: 6, fontSize: 12.5, fontFamily: sx.sans, fontWeight: 600, display: 'flex', alignItems: 'center', gap: 7, cursor: 'pointer' }}>
              <SXIcon name="plus" size={11} color={sx.bg}/> New campaign
            </button>
          }
        />

        <div style={{ flex: 1, display: 'grid', gridTemplateColumns: '260px 1fr', overflow: 'hidden' }}>
          {/* Campaign list rail */}
          <div style={{ borderRight: `1px solid ${sx.rule}`, background: sx.bgSunken, padding: '18px 14px', overflow: 'auto' }}>
            <div style={{ fontFamily: sx.mono, fontSize: 10, color: sx.textFaint, letterSpacing: '0.15em', textTransform: 'uppercase', marginBottom: 14, padding: '0 4px' }}>All campaigns · {d.campaigns.length}</div>
            {d.campaigns.map((c, i) => (
              <div key={c.slug} style={{
                padding: '12px 14px', borderRadius: 8, marginBottom: 4,
                background: i === 0 ? sx.bgRaised : 'transparent',
                border: `1px solid ${i === 0 ? sx.ruleStrong : 'transparent'}`,
                display: 'flex', alignItems: 'flex-start', gap: 11, cursor: 'pointer',
              }}>
                <div style={{ width: 3, height: 38, background: c.color, borderRadius: 1.5, flexShrink: 0, marginTop: 2 }}/>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontFamily: sx.serif, fontSize: 15, color: sx.text }}>{c.name}</div>
                  <div style={{ fontFamily: sx.mono, fontSize: 10, color: sx.textFaint, marginTop: 3, letterSpacing: '0.08em' }}>{c.system.toUpperCase()}</div>
                  <div style={{ fontFamily: sx.mono, fontSize: 11, color: sx.textDim, marginTop: 6 }}>{c.sessions} sessions</div>
                </div>
              </div>
            ))}
            <button style={{
              width: '100%', padding: '10px 12px', marginTop: 10, borderRadius: 8,
              background: 'transparent', border: `1px dashed ${sx.rule}`,
              color: sx.textDim, fontSize: 12, fontFamily: sx.sans, cursor: 'pointer',
            }}>+ New campaign</button>
          </div>

          {/* Detail */}
          <div style={{ overflow: 'auto', padding: '28px 32px 32px' }}>
            {/* Hero */}
            <div style={{ paddingBottom: 24, borderBottom: `1px solid ${sx.rule}`, marginBottom: 28 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, fontFamily: sx.mono, fontSize: 10, color: sx.textFaint, letterSpacing: '0.18em', textTransform: 'uppercase', marginBottom: 12 }}>
                <span style={{ width: 8, height: 8, borderRadius: '50%', background: active.color }}/>
                <span style={{ color: sx.textDim }}>{active.system}</span>
                <span>·</span>
                <span>slug · <span style={{ color: sx.text }}>{active.slug}</span></span>
              </div>
              <h1 style={{ fontFamily: sx.serif, fontWeight: 300, fontSize: 44, color: sx.text, margin: 0, lineHeight: 1.05, letterSpacing: '-0.025em' }}>
                {active.name}
              </h1>
              <p style={{ fontFamily: sx.serif, fontSize: 15.5, fontStyle: 'italic', color: sx.textDim, marginTop: 14, marginBottom: 0, maxWidth: 620, lineHeight: 1.5 }}>
                A Delta Green campaign. The investigators are trying to find Abigail Wright before something else does.
              </p>

              <div style={{ display: 'flex', gap: 32, marginTop: 20 }}>
                {[
                  { k: 'Sessions',  v: active.sessions },
                  { k: 'Hours',     v: '28h 14m' },
                  { k: 'Words',     v: '212K' },
                  { k: 'Players',   v: '5' },
                  { k: 'Last met',  v: 'Apr 28' },
                ].map((s) => (
                  <div key={s.k}>
                    <div style={{ fontFamily: sx.mono, fontSize: 10, color: sx.textFaint, letterSpacing: '0.15em', textTransform: 'uppercase', marginBottom: 4 }}>{s.k}</div>
                    <div style={{ fontFamily: sx.serif, fontSize: 20, color: sx.text, fontWeight: 300 }}>{s.v}</div>
                  </div>
                ))}
              </div>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1.4fr 1fr', gap: 36 }}>
              {/* Sessions / episodes */}
              <div>
                <SXSH kicker="Episodes" action={<a style={{ fontFamily: sx.mono, fontSize: 11, color: sx.textDim }}>+ ADD EPISODE</a>}>
                  The thread
                </SXSH>
                <div style={{ borderTop: `1px solid ${sx.rule}` }}>
                  {[
                    { n: 'S1 E7', title: 'The Yellow Throne',     date: 'Apr 28', state: 'recording'  },
                    { n: 'S1 E6', title: 'Statues That Weep',     date: 'Apr 21', state: 'transcribed' },
                    { n: 'S1 E5', title: 'A Long Bright Hallway', date: 'Apr 14', state: 'transcribed' },
                    { n: 'S1 E4', title: 'The Macallistar House', date: 'Apr 07', state: 'transcribed' },
                    { n: 'S1 E3', title: 'Kings and Queens',      date: 'Mar 31', state: 'transcribed' },
                    { n: 'S1 E2', title: 'Cumstone, Year One',    date: 'Mar 24', state: 'transcribed' },
                    { n: 'S1 E1', title: 'Remove Your Mask',      date: 'Mar 17', state: 'transcribed' },
                  ].map((ep, i, arr) => (
                    <div key={i} onClick={() => onNavigate && onNavigate('TranscriptDetail')} style={{
                      display: 'grid', gridTemplateColumns: '60px 1fr 90px 120px',
                      gap: 16, padding: '14px 0', borderBottom: `1px solid ${sx.rule}`,
                      alignItems: 'center', cursor: 'pointer',
                    }}>
                      <div style={{ fontFamily: sx.mono, fontSize: 11, color: sx.textFaint, letterSpacing: '0.06em' }}>{ep.n}</div>
                      <div style={{ fontFamily: sx.serif, fontSize: 16, color: sx.text }}>{ep.title}</div>
                      <div style={{ fontFamily: sx.mono, fontSize: 11, color: sx.textFaint, textAlign: 'right' }}>{ep.date}</div>
                      <div>
                        {ep.state === 'recording' ? (
                          <span style={{ fontFamily: sx.mono, fontSize: 10, padding: '3px 8px', borderRadius: 3, background: sx.rose + '15', color: sx.rose, border: `1px solid ${sx.rose}40`, letterSpacing: '0.12em', display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                            <span style={{ width: 5, height: 5, borderRadius: '50%', background: sx.rose, boxShadow: `0 0 5px ${sx.rose}` }}/>
                            LIVE
                          </span>
                        ) : (
                          <span style={{ fontFamily: sx.mono, fontSize: 10, padding: '3px 8px', borderRadius: 3, background: sx.green + '12', color: sx.green, border: `1px solid ${sx.green}30`, letterSpacing: '0.12em' }}>TRANSCRIBED</span>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              {/* Roster + settings */}
              <div>
                {/* Roster */}
                <SXSH kicker="Roster" action={<a style={{ fontFamily: sx.mono, fontSize: 11, color: sx.cyan }}>+ ADD MEMBER</a>}>
                  At this table
                </SXSH>
                <div style={{ borderTop: `1px solid ${sx.rule}`, marginBottom: 32 }}>
                  {d.speakers.slice(0, 5).map((s, i) => (
                    <div key={s.name} style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '12px 0', borderBottom: `1px solid ${sx.rule}` }}>
                      <div style={{
                        width: 30, height: 30, borderRadius: '50%',
                        background: `oklch(0.5 0.12 ${s.hue})`,
                        color: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'center',
                        fontFamily: sx.mono, fontSize: 11, fontWeight: 600,
                      }}>{s.initials}</div>
                      <div style={{ flex: 1 }}>
                        <div style={{ fontFamily: sx.serif, fontSize: 14, color: sx.text }}>{s.name}</div>
                        <div style={{ fontFamily: sx.mono, fontSize: 10, color: sx.textFaint, letterSpacing: '0.1em', marginTop: 2 }}>
                          {s.role.toUpperCase()} · CHAR: <span style={{ color: sx.textDim }}>{['—', 'Marcus Vance', 'Priya Roth', 'Devon Hall', 'Yuki Tanaka'][i]}</span>
                        </div>
                      </div>
                      <span style={{ fontFamily: sx.mono, fontSize: 10, color: sx.green, letterSpacing: '0.1em' }}>● BOUND</span>
                    </div>
                  ))}
                </div>

                {/* Campaign defaults */}
                <SXSH kicker="Defaults">Settings</SXSH>
                <div style={{ borderTop: `1px solid ${sx.rule}` }}>
                  {[
                    { k: 'Discord channel', v: '#table-1 (123…345678)', mono: true },
                    { k: 'Vocabulary',      v: '5 terms · Macallistar, Hastur …' },
                    { k: 'Default model',   v: 'large-v3-turbo', mono: true },
                    { k: 'Output folder',   v: './transcripts/impossible-landscapes/', mono: true },
                    { k: 'Auto-refine',     v: 'On', accent: sx.green },
                    { k: 'Auto-summarize',  v: 'On', accent: sx.green },
                  ].map((row, i) => (
                    <div key={i} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', padding: '12px 0', borderBottom: `1px solid ${sx.rule}`, gap: 12 }}>
                      <span style={{ fontFamily: sx.serif, fontSize: 14, color: sx.text, fontStyle: 'italic' }}>{row.k}</span>
                      <span style={{ fontFamily: row.mono ? sx.mono : sx.sans, fontSize: row.mono ? 11.5 : 13, color: row.accent || sx.textDim, textAlign: 'right' }}>{row.v}</span>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

// --- Config ---
const StudioConfig = ({ onNavigate }) => {
  const d = window.WISPER_DATA;
  const [active, setActive] = React.useState('Transcription');
  const sections = ['Transcription', 'LLM', 'Discord', 'Storage', 'About'];
  return (
    <div data-screen-label="Studio · Config" style={{
      width: '100%', height: '100%', background: sx.bg, color: sx.text,
      fontFamily: sx.sans, overflow: 'hidden', display: 'flex',
    }}>
      <SXSidebar active="Config" onNavigate={onNavigate}/>
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        <SXToolbar kicker="Config" title="System" sub="LOCAL · ~/.wisper/config.toml"
          actions={
            <button style={{ background: sx.bgRaised, border: `1px solid ${sx.ruleStrong}`, color: sx.text, padding: '7px 13px', borderRadius: 6, fontSize: 12.5, fontFamily: sx.sans, cursor: 'pointer' }}>
              Open in editor
            </button>
          }
        />

        <div style={{ flex: 1, display: 'grid', gridTemplateColumns: '220px 1fr', overflow: 'hidden' }}>
          {/* Section nav */}
          <div style={{ padding: '20px 14px', borderRight: `1px solid ${sx.rule}`, background: sx.bgSunken }}>
            <div style={{ fontFamily: sx.mono, fontSize: 10, color: sx.textFaint, letterSpacing: '0.15em', textTransform: 'uppercase', padding: '0 4px 12px' }}>Sections</div>
            {sections.map((sec) => (
              <a key={sec} onClick={() => setActive(sec)} style={{
                display: 'block', padding: '8px 12px', borderRadius: 6, marginBottom: 2,
                fontFamily: sx.serif, fontStyle: active === sec ? 'italic' : 'normal',
                fontSize: 15, color: active === sec ? sx.text : sx.textDim,
                background: active === sec ? sx.bgRaised : 'transparent',
                cursor: 'pointer',
                borderLeft: active === sec ? `2px solid ${sx.cyan}` : '2px solid transparent',
                paddingLeft: active === sec ? 10 : 12,
              }}>{sec}</a>
            ))}
          </div>

          {/* Active section */}
          <div style={{ overflow: 'auto', padding: '32px 36px' }}>
            {active === 'Transcription' && (
              <div>
                <SXSH kicker="Transcription">Whisper &amp; pyannote</SXSH>
                <div style={{ borderTop: `1px solid ${sx.rule}` }}>
                  {[
                    { k: 'HuggingFace token', desc: 'Required for pyannote diarization', v: <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}><span style={{ fontFamily: sx.mono, fontSize: 12, color: sx.green }}>✓ hf_••••••••••••••••K2pQ</span><a style={{ color: sx.textDim, fontSize: 11, fontFamily: sx.mono, cursor: 'pointer' }}>rotate</a></span> },
                    { k: 'Default whisper model', desc: 'Tradeoff between speed and accuracy', v: <select style={{ background: sx.bgRaised, color: sx.text, border: `1px solid ${sx.ruleStrong}`, padding: '6px 10px', borderRadius: 5, fontSize: 12, fontFamily: sx.mono }}><option>large-v3-turbo</option></select> },
                    { k: 'Compute',             desc: 'Device + precision', v: <span style={{ fontFamily: sx.mono, fontSize: 12, color: sx.text }}>cuda:0 · float16</span> },
                    { k: 'Beam size',           desc: 'Decoding search width', v: <span style={{ fontFamily: sx.mono, fontSize: 12, color: sx.text }}>5</span> },
                    { k: 'Parallel diarize',    desc: 'Run diarization concurrent to transcription', v: <span style={{ fontFamily: sx.mono, fontSize: 12, color: sx.green }}>● ON</span> },
                  ].map((row, i) => (
                    <div key={i} style={{ display: 'grid', gridTemplateColumns: '1fr auto', gap: 24, padding: '18px 0', borderBottom: `1px solid ${sx.rule}`, alignItems: 'center' }}>
                      <div>
                        <div style={{ fontFamily: sx.serif, fontSize: 16, color: sx.text }}>{row.k}</div>
                        <div style={{ fontSize: 12, color: sx.textFaint, marginTop: 4 }}>{row.desc}</div>
                      </div>
                      <div>{row.v}</div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {active === 'LLM' && (
              <div>
                <SXSH kicker="LLM">Refine &amp; summarize</SXSH>
                <p style={{ fontFamily: sx.serif, fontStyle: 'italic', color: sx.textDim, fontSize: 14.5, maxWidth: 620, marginTop: 0, lineHeight: 1.55 }}>Used for vocabulary correction, unknown-speaker ID, session recap and DM notes. Anything provider-agnostic works.</p>

                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 8, marginTop: 20, marginBottom: 24 }}>
                  {[
                    { n: 'Ollama',     status: 'connected', model: 'llama3.1:70b', active: true },
                    { n: 'LM Studio',  status: 'idle' },
                    { n: 'Anthropic',  status: 'idle' },
                    { n: 'OpenAI',     status: 'idle' },
                    { n: 'Google',     status: 'idle' },
                  ].map((p) => (
                    <div key={p.n} style={{
                      padding: '14px 12px', borderRadius: 8,
                      border: `1px solid ${p.active ? sx.cyan + '50' : sx.rule}`,
                      background: p.active ? sx.cyan + '08' : sx.bgRaised,
                    }}>
                      <div style={{ fontFamily: sx.serif, fontSize: 14, color: sx.text }}>{p.n}</div>
                      <div style={{ fontFamily: sx.mono, fontSize: 10, color: p.active ? sx.cyan : sx.textFaint, letterSpacing: '0.12em', marginTop: 6 }}>
                        {p.active ? '● ACTIVE' : '○ IDLE'}
                      </div>
                      {p.model && <div style={{ fontFamily: sx.mono, fontSize: 10, color: sx.textDim, marginTop: 4 }}>{p.model}</div>}
                    </div>
                  ))}
                </div>

                <div style={{ borderTop: `1px solid ${sx.rule}` }}>
                  {[
                    { k: 'Active provider', v: <span style={{ fontFamily: sx.mono, fontSize: 12, color: sx.text }}>ollama</span> },
                    { k: 'Model',           v: <span style={{ fontFamily: sx.mono, fontSize: 12, color: sx.text }}>llama3.1:70b</span> },
                    { k: 'Endpoint',        v: <span style={{ fontFamily: sx.mono, fontSize: 12, color: sx.text }}>http://localhost:11434</span> },
                    { k: 'Health',          v: <span style={{ fontFamily: sx.mono, fontSize: 12, color: sx.green }}>● 47 ms · last check 12s ago</span> },
                    { k: 'Refine prompt',   v: <a style={{ fontFamily: sx.mono, fontSize: 11, color: sx.cyan }}>view default →</a> },
                    { k: 'Summary schema',  v: <a style={{ fontFamily: sx.mono, fontSize: 11, color: sx.cyan }}>view default →</a> },
                  ].map((row, i) => (
                    <div key={i} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', padding: '14px 0', borderBottom: `1px solid ${sx.rule}` }}>
                      <span style={{ fontFamily: sx.serif, fontSize: 15, color: sx.text }}>{row.k}</span>
                      <span>{row.v}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {active === 'Discord' && (
              <div>
                <SXSH kicker="Discord">Recording bot</SXSH>
                <div style={{ display: 'flex', alignItems: 'center', gap: 14, marginBottom: 24, padding: '14px 16px', background: sx.green + '08', border: `1px solid ${sx.green}30`, borderRadius: 6 }}>
                  <span style={{ width: 10, height: 10, borderRadius: '50%', background: sx.green, boxShadow: `0 0 8px ${sx.green}` }}/>
                  <div>
                    <div style={{ fontFamily: sx.serif, fontSize: 14.5, color: sx.text }}>Bot is online · 2 guilds</div>
                    <div style={{ fontFamily: sx.mono, fontSize: 11, color: sx.textDim, marginTop: 3 }}>JDA 5.4 · sidecar · gateway latency 18 ms</div>
                  </div>
                </div>

                <div style={{ borderTop: `1px solid ${sx.rule}`, marginBottom: 32 }}>
                  {[
                    { k: 'Bot token',       v: <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}><span style={{ fontFamily: sx.mono, fontSize: 12, color: sx.green }}>✓ MTk5···set</span><a style={{ color: sx.textDim, fontSize: 11, fontFamily: sx.mono, cursor: 'pointer' }}>rotate</a></span> },
                    { k: 'Default guild',   v: <span style={{ fontFamily: sx.mono, fontSize: 12, color: sx.text }}>My Server (987…65432)</span> },
                    { k: 'Default channel', v: <span style={{ fontFamily: sx.mono, fontSize: 12, color: sx.text }}>#table-1 (123…45678)</span> },
                    { k: 'Audio sink',      v: <span style={{ fontFamily: sx.mono, fontSize: 12, color: sx.text }}>Ogg/Opus · 60s rotation · packet-count crash-safe</span> },
                    { k: 'Auto-rejoin',     v: <span style={{ fontFamily: sx.mono, fontSize: 12, color: sx.text }}>[2, 5, 15, 30, 60]s backoff</span> },
                  ].map((row, i) => (
                    <div key={i} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', padding: '14px 0', borderBottom: `1px solid ${sx.rule}` }}>
                      <span style={{ fontFamily: sx.serif, fontSize: 15, color: sx.text }}>{row.k}</span>
                      <span>{row.v}</span>
                    </div>
                  ))}
                </div>

                <SXSH kicker="Quick-connect" action={<a style={{ fontFamily: sx.mono, fontSize: 11, color: sx.cyan }}>+ ADD PRESET</a>}>Presets</SXSH>
                <div style={{ borderTop: `1px solid ${sx.rule}` }}>
                  {d.discordPresets.map((p) => (
                    <div key={p.name} style={{ display: 'grid', gridTemplateColumns: '1fr auto auto', gap: 16, padding: '14px 0', borderBottom: `1px solid ${sx.rule}`, alignItems: 'center' }}>
                      <div>
                        <div style={{ fontFamily: sx.serif, fontSize: 15, color: sx.text }}>{p.name}</div>
                        <div style={{ fontFamily: sx.mono, fontSize: 10, color: sx.textFaint, marginTop: 3 }}>guild {p.guild} · channel {p.channel}</div>
                      </div>
                      <button style={{ background: sx.bgRaised, border: `1px solid ${sx.rule}`, color: sx.text, padding: '5px 11px', borderRadius: 5, fontSize: 11.5, fontFamily: sx.sans, cursor: 'pointer' }}>Set default</button>
                      <a style={{ fontFamily: sx.mono, fontSize: 11, color: sx.textFaint, cursor: 'pointer' }}>remove</a>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {active === 'Storage' && (
              <div>
                <SXSH kicker="Storage">Files &amp; paths</SXSH>
                <div style={{ borderTop: `1px solid ${sx.rule}` }}>
                  {[
                    { k: 'Recordings dir',  v: '~/.wisper/recordings/' },
                    { k: 'Transcripts dir', v: '~/.wisper/transcripts/' },
                    { k: 'Profiles dir',    v: '~/.wisper/profiles/' },
                    { k: 'Embeddings',      v: '~/.wisper/profiles/embeddings/*.npy' },
                    { k: 'Whisper models',  v: '~/.cache/huggingface/hub/' },
                    { k: 'Logs',            v: './logs/wisper_<ts>.log' },
                  ].map((row, i) => (
                    <div key={i} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', padding: '14px 0', borderBottom: `1px solid ${sx.rule}` }}>
                      <span style={{ fontFamily: sx.serif, fontSize: 15, color: sx.text }}>{row.k}</span>
                      <span style={{ fontFamily: sx.mono, fontSize: 12, color: sx.textDim }}>{row.v}</span>
                    </div>
                  ))}
                </div>
                <div style={{ marginTop: 28 }}>
                  <SXSH kicker="On disk">Currently using</SXSH>
                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 0, borderTop: `1px solid ${sx.rule}`, borderBottom: `1px solid ${sx.rule}` }}>
                    {[
                      { k: 'Recordings',  v: '14.2 GB', sub: '7 files' },
                      { k: 'Transcripts', v: '142 MB',  sub: '38 files' },
                      { k: 'Profiles',    v: '24 KB',   sub: '6 embeddings' },
                      { k: 'Models',      v: '3.1 GB',  sub: 'large-v3-turbo · pyannote 3.1' },
                    ].map((s, i) => (
                      <div key={i} style={{ padding: '16px 18px', borderLeft: i ? `1px solid ${sx.rule}` : 'none' }}>
                        <div style={{ fontFamily: sx.mono, fontSize: 10, color: sx.textFaint, letterSpacing: '0.12em', textTransform: 'uppercase', marginBottom: 6 }}>{s.k}</div>
                        <div style={{ fontFamily: sx.serif, fontSize: 22, color: sx.text, fontWeight: 300 }}>{s.v}</div>
                        <div style={{ fontFamily: sx.mono, fontSize: 11, color: sx.textFaint, marginTop: 4 }}>{s.sub}</div>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            )}

            {active === 'About' && (
              <div>
                <SXSH kicker="About">wisper-transcribe</SXSH>
                <div style={{ display: 'flex', alignItems: 'center', gap: 18, padding: '20px 0', borderTop: `1px solid ${sx.rule}`, borderBottom: `1px solid ${sx.rule}`, marginBottom: 24 }}>
                  <img src="assets/logo.png" width="72" height="72" style={{ borderRadius: 12 }}/>
                  <div>
                    <div style={{ fontFamily: sx.serif, fontStyle: 'italic', fontSize: 28, color: sx.text }}>wisper</div>
                    <div style={{ fontFamily: sx.mono, fontSize: 11, color: sx.textFaint, letterSpacing: '0.1em', marginTop: 6 }}>v0.7.2 · LOCAL-FIRST TTRPG TRANSCRIPTION</div>
                    <div style={{ fontFamily: sx.serif, fontStyle: 'italic', fontSize: 14, color: sx.textDim, marginTop: 8, maxWidth: 480 }}>Local podcast transcription with automatic speaker identification. Built for 5–8 voice tabletop RPG recordings.</div>
                  </div>
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 28 }}>
                  <div>
                    <div style={{ fontFamily: sx.mono, fontSize: 10, color: sx.textFaint, letterSpacing: '0.15em', textTransform: 'uppercase', marginBottom: 10 }}>Components</div>
                    {[
                      ['faster-whisper', '1.0.3'],
                      ['pyannote.audio', '3.1.1'],
                      ['mlx-whisper', '0.4.0'],
                      ['fastapi', '0.115'],
                      ['JDA sidecar', '0.1.8'],
                    ].map(([k, v]) => (
                      <div key={k} style={{ display: 'flex', justifyContent: 'space-between', padding: '7px 0', fontFamily: sx.mono, fontSize: 11.5 }}>
                        <span style={{ color: sx.textDim }}>{k}</span>
                        <span style={{ color: sx.text }}>{v}</span>
                      </div>
                    ))}
                  </div>
                  <div>
                    <div style={{ fontFamily: sx.mono, fontSize: 10, color: sx.textFaint, letterSpacing: '0.15em', textTransform: 'uppercase', marginBottom: 10 }}>Links</div>
                    {[
                      'README',
                      'Architecture',
                      'Open issue',
                      'pyannote/speaker-diarization-3.1',
                    ].map((l) => (
                      <a key={l} style={{ display: 'block', padding: '7px 0', fontSize: 13, color: sx.cyan, fontFamily: sx.serif, fontStyle: 'italic', cursor: 'pointer' }}>{l} →</a>
                    ))}
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};

window.StudioCampaigns = StudioCampaigns;
window.StudioConfig = StudioConfig;
