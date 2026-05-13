// Studio · Recordings list + Speakers (voice profiles).

const sm = window.studioTokens;
const SMIcon = window.SIcon;
const SMSidebar = window.StudioSidebar;
const SMSH = window.StudioSectionHead;
const SMToolbar = window.StudioToolbar;

// --- Recordings list ---
const StudioRecordings = ({ onNavigate }) => {
  const d = window.WISPER_DATA;
  return (
    <div data-screen-label="Studio · Recordings" style={{
      width: '100%', height: '100%', background: sm.bg, color: sm.text,
      fontFamily: sm.sans, overflow: 'hidden', display: 'flex',
    }}>
      <SMSidebar active="Record" onNavigate={onNavigate}/>
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        <SMToolbar kicker="Recordings" title="Captured sessions" sub="7 RAW RECORDINGS · 1 LIVE"
          actions={
            <button onClick={() => onNavigate && onNavigate('Record')} style={{ background: sm.cyan, color: sm.bg, border: 'none', padding: '7px 14px', borderRadius: 6, fontSize: 12.5, fontFamily: sm.sans, fontWeight: 600, display: 'flex', alignItems: 'center', gap: 7, cursor: 'pointer', boxShadow: `0 0 20px ${sm.cyan}30` }}>
              <SMIcon name="record" size={10} color={sm.bg}/> Start session
            </button>
          }
        />

        <div style={{ flex: 1, padding: '24px 28px', overflow: 'auto' }}>
          {/* Filter row */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 4, marginBottom: 16, flexWrap: 'wrap' }}>
            {[
              { label: 'All',           count: 7,  active: true },
              { label: 'Live',          count: 1,  color: sm.rose },
              { label: 'Transcribing',  count: 1,  color: sm.cyan },
              { label: 'Done',          count: 4,  color: sm.green },
              { label: 'Failed',        count: 1,  color: sm.amber },
            ].map((f, i) => (
              <button key={f.label} style={{
                padding: '5px 12px', borderRadius: 999,
                background: f.active ? sm.bgRaised : 'transparent',
                border: `1px solid ${f.active ? sm.ruleStrong : sm.rule}`,
                color: f.active ? sm.text : sm.textDim, fontSize: 12, fontFamily: sm.sans,
                display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer',
              }}>
                {f.color && <span style={{ width: 6, height: 6, borderRadius: '50%', background: f.color, boxShadow: f.color === sm.rose ? `0 0 6px ${f.color}` : 'none' }}/>}
                {f.label}
                <span style={{ fontFamily: sm.mono, fontSize: 10.5, color: sm.textFaint }}>{f.count}</span>
              </button>
            ))}
            <div style={{ flex: 1 }}/>
            <div style={{
              display: 'flex', alignItems: 'center', gap: 8, padding: '6px 10px',
              background: sm.bgRaised, border: `1px solid ${sm.rule}`, borderRadius: 6,
              width: 240, fontSize: 12, color: sm.textFaint,
            }}>
              <SMIcon name="search" size={11} color={sm.textFaint}/>
              <span>Filter recordings…</span>
            </div>
          </div>

          {/* Group: live */}
          <div style={{ marginBottom: 24 }}>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 14, marginBottom: 12 }}>
              <div style={{ fontFamily: sm.mono, fontSize: 10, color: sm.rose, letterSpacing: '0.18em', textTransform: 'uppercase', display: 'flex', alignItems: 'center', gap: 8 }}>
                <span style={{ width: 6, height: 6, borderRadius: '50%', background: sm.rose, boxShadow: `0 0 6px ${sm.rose}` }}/>
                Live now
              </div>
              <div style={{ flex: 1, height: 1, background: sm.rule }}/>
            </div>

            {d.recordings.filter(r => r.status === 'recording').map((r) => {
              const camp = d.campaigns.find(c => c.slug === r.campaign);
              return (
                <div key={r.id} onClick={() => onNavigate && onNavigate('Record')} style={{
                  padding: '18px 20px', borderRadius: 8,
                  background: `linear-gradient(90deg, ${sm.rose}10, transparent 60%)`,
                  border: `1px solid ${sm.rose}40`,
                  display: 'grid', gridTemplateColumns: '1fr auto auto auto auto', gap: 24, alignItems: 'center', cursor: 'pointer',
                }}>
                  <div>
                    <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, marginBottom: 6 }}>
                      <span style={{ fontFamily: sm.mono, fontSize: 10, color: sm.rose, letterSpacing: '0.18em', fontWeight: 600 }}>RECORDING · {r.duration}</span>
                    </div>
                    <div style={{ fontFamily: sm.serif, fontSize: 22, color: sm.text }}>{r.name}</div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 6, fontSize: 12, color: sm.textDim }}>
                      <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                        <span style={{ width: 6, height: 6, borderRadius: '50%', background: camp.color }}/> {camp.name}
                      </span>
                      <span style={{ fontFamily: sm.mono, fontSize: 11, color: sm.textFaint }}>{r.channel} · {r.id}</span>
                    </div>
                  </div>
                  <div>
                    <div style={{ fontFamily: sm.serif, fontSize: 20, color: sm.text, lineHeight: 1 }}>{r.segments}</div>
                    <div style={{ fontFamily: sm.mono, fontSize: 9.5, color: sm.textFaint, letterSpacing: '0.12em', marginTop: 4 }}>SEGMENTS</div>
                  </div>
                  <div>
                    <div style={{ fontFamily: sm.serif, fontSize: 20, color: sm.cyan, lineHeight: 1 }}>5</div>
                    <div style={{ fontFamily: sm.mono, fontSize: 9.5, color: sm.textFaint, letterSpacing: '0.12em', marginTop: 4 }}>VOICES</div>
                  </div>
                  <div style={{ display: 'flex', gap: 2, alignItems: 'flex-end', height: 28, width: 90 }}>
                    {Array.from({ length: 30 }).map((_, i) => {
                      const h = 0.15 + 0.7 * Math.abs(Math.sin(i * 0.5));
                      return <div key={i} style={{ flex: 1, height: `${Math.round(h * 100)}%`, background: sm.rose, borderRadius: 1, opacity: 0.6 }}/>;
                    })}
                  </div>
                  <button onClick={(e) => { e.stopPropagation(); onNavigate && onNavigate('Record'); }} style={{ background: sm.bgRaised, border: `1px solid ${sm.ruleStrong}`, color: sm.text, padding: '8px 14px', borderRadius: 6, fontSize: 12.5, fontFamily: sm.sans, cursor: 'pointer' }}>
                    Open live view →
                  </button>
                </div>
              );
            })}
          </div>

          {/* Group: completed/transcribing/failed */}
          <div>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 14, marginBottom: 12 }}>
              <div style={{ fontFamily: sm.mono, fontSize: 10, color: sm.textFaint, letterSpacing: '0.18em', textTransform: 'uppercase' }}>Captured</div>
              <div style={{ flex: 1, height: 1, background: sm.rule }}/>
              <div style={{ fontFamily: sm.mono, fontSize: 10, color: sm.textFaint, letterSpacing: '0.12em' }}>{d.recordings.length - 1} ITEMS</div>
            </div>

            {/* Headers */}
            <div style={{
              display: 'grid', gridTemplateColumns: '12px 1.4fr 1fr 90px 70px 110px 130px',
              gap: 16, padding: '8px 16px', borderTop: `1px solid ${sm.rule}`, borderBottom: `1px solid ${sm.rule}`,
              fontFamily: sm.mono, fontSize: 10, color: sm.textFaint, letterSpacing: '0.12em',
            }}>
              <div></div><div>SESSION</div><div>CAMPAIGN · CHANNEL</div><div style={{ textAlign: 'right' }}>DURATION</div><div style={{ textAlign: 'right' }}>SEG</div><div>STATUS</div><div style={{ textAlign: 'right' }}>ACTION</div>
            </div>

            {d.recordings.filter(r => r.status !== 'recording').map((r, i, arr) => {
              const camp = d.campaigns.find(c => c.slug === r.campaign);
              const statusColors = {
                done:         { c: sm.green, label: 'TRANSCRIBED' },
                transcribing: { c: sm.cyan,  label: 'TRANSCRIBING' },
                failed:       { c: sm.amber, label: 'FAILED' },
              };
              const sc = statusColors[r.status];
              return (
                <div key={r.id} style={{
                  display: 'grid', gridTemplateColumns: '12px 1.4fr 1fr 90px 70px 110px 130px',
                  gap: 16, padding: '14px 16px',
                  borderBottom: i < arr.length - 1 ? `1px solid ${sm.rule}` : 'none',
                  alignItems: 'center',
                }}>
                  <SMIcon name="waveform" size={11} color={sm.textFaint}/>
                  <div>
                    <div style={{ fontFamily: sm.serif, fontSize: 15, color: sm.text }}>{r.name}</div>
                    <div style={{ fontFamily: sm.mono, fontSize: 10, color: sm.textFaint, marginTop: 3 }}>{r.id} · started {r.startedAt}</div>
                  </div>
                  <div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 7, fontSize: 12.5, color: sm.textDim }}>
                      <span style={{ width: 5, height: 5, borderRadius: '50%', background: camp.color }}/>
                      {camp.name}
                    </div>
                    <div style={{ fontFamily: sm.mono, fontSize: 10.5, color: sm.textFaint, marginTop: 3 }}>{r.channel}</div>
                  </div>
                  <div style={{ fontFamily: sm.mono, fontSize: 12, color: sm.textDim, textAlign: 'right' }}>{r.duration}</div>
                  <div style={{ fontFamily: sm.mono, fontSize: 11, color: sm.textFaint, textAlign: 'right' }}>{r.segments}</div>
                  <div>
                    {r.status === 'transcribing' ? (
                      <div>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 7, fontFamily: sm.mono, fontSize: 10, color: sc.c, letterSpacing: '0.12em' }}>
                          <span style={{ width: 6, height: 6, borderRadius: '50%', background: sc.c, boxShadow: `0 0 5px ${sc.c}` }}/>
                          {sc.label}
                        </div>
                        <div style={{ height: 2, background: sm.bgSunken, marginTop: 6, borderRadius: 1, overflow: 'hidden' }}>
                          <div style={{ width: '34%', height: '100%', background: sc.c }}/>
                        </div>
                      </div>
                    ) : (
                      <span style={{
                        fontFamily: sm.mono, fontSize: 10, letterSpacing: '0.12em',
                        padding: '3px 8px', border: `1px solid ${sc.c}40`,
                        background: sc.c + '12', color: sc.c, borderRadius: 3,
                      }}>{sc.label}</span>
                    )}
                    {r.error && <div style={{ fontFamily: sm.mono, fontSize: 10, color: sm.amber, marginTop: 4, fontStyle: 'italic' }}>{r.error}</div>}
                  </div>
                  <div style={{ textAlign: 'right' }}>
                    {r.status === 'done' && (
                      <button onClick={() => onNavigate && onNavigate('TranscriptDetail')} style={{ background: 'transparent', border: 'none', color: sm.text, fontSize: 12, fontFamily: sm.sans, cursor: 'pointer', display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                        Open transcript →
                      </button>
                    )}
                    {r.status === 'transcribing' && (
                      <button onClick={() => onNavigate && onNavigate('Dashboard')} style={{ background: 'transparent', border: 'none', color: sm.cyan, fontSize: 12, fontFamily: sm.mono, cursor: 'pointer' }}>view job →</button>
                    )}
                    {r.status === 'failed' && (
                      <button onClick={() => onNavigate && onNavigate('Transcribe')} style={{ background: 'transparent', border: `1px solid ${sm.amber}40`, color: sm.amber, fontSize: 11.5, padding: '5px 10px', borderRadius: 5, fontFamily: sm.sans, cursor: 'pointer' }}>Retry transcribe</button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
};

// --- Speakers (voice profiles) ---
const StudioSpeakers = ({ onNavigate }) => {
  const d = window.WISPER_DATA;
  return (
    <div data-screen-label="Studio · Speakers" style={{
      width: '100%', height: '100%', background: sm.bg, color: sm.text,
      fontFamily: sm.sans, overflow: 'hidden', display: 'flex',
    }}>
      <SMSidebar active="Speakers" onNavigate={onNavigate}/>
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        <SMToolbar kicker="Speakers" title="Voice profiles" sub="6 ENROLLED · 512-DIM EMBEDDINGS"
          actions={
            <button style={{ background: sm.cyan, color: sm.bg, border: 'none', padding: '7px 14px', borderRadius: 6, fontSize: 12.5, fontFamily: sm.sans, fontWeight: 600, display: 'flex', alignItems: 'center', gap: 7, cursor: 'pointer' }}>
              <SMIcon name="plus" size={11} color={sm.bg}/> Enroll a voice
            </button>
          }
        />

        <div style={{ flex: 1, padding: '24px 28px', overflow: 'auto' }}>
          {/* Intro strip */}
          <div style={{ paddingBottom: 20, borderBottom: `1px solid ${sm.rule}`, marginBottom: 24 }}>
            <p style={{ fontFamily: sm.serif, fontSize: 16, fontStyle: 'italic', color: sm.textDim, margin: 0, maxWidth: 720, lineHeight: 1.55 }}>
              Voice embeddings are extracted with pyannote and matched by cosine similarity. The more sessions a profile blends in, the sharper it gets.
            </p>
          </div>

          {/* Grid of speakers */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 1, background: sm.rule, border: `1px solid ${sm.rule}` }}>
            {d.speakers.map((s, i) => {
              const sim = [0.91, 0.87, 0.93, 0.84, 0.79, 0.76][i];
              const lastHeard = ['just now', '14 min ago', '46 min ago', '2 days ago', '6 days ago', '12 days ago'][i];
              return (
                <div key={s.name} style={{ padding: '22px 24px', background: sm.bg }}>
                  <div style={{ display: 'flex', alignItems: 'flex-start', gap: 16 }}>
                    <div style={{
                      width: 56, height: 56, borderRadius: '50%',
                      background: `oklch(0.5 0.12 ${s.hue})`,
                      color: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'center',
                      fontFamily: sm.serif, fontStyle: 'italic', fontSize: 24, fontWeight: 500,
                      flexShrink: 0,
                    }}>{s.initials}</div>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 12 }}>
                        <div>
                          <div style={{ fontFamily: sm.serif, fontSize: 22, color: sm.text }}>{s.name}</div>
                          <div style={{ fontFamily: sm.mono, fontSize: 10.5, color: sm.textFaint, letterSpacing: '0.1em', marginTop: 3 }}>{s.role.toUpperCase()} · DISCORD #{(341278901 + i * 137).toString().slice(0, 8)}…</div>
                        </div>
                        <button style={{ background: 'transparent', border: 'none', color: sm.textFaint, fontSize: 18, cursor: 'pointer' }}>⋯</button>
                      </div>

                      {/* Waveform sample */}
                      <div style={{ display: 'flex', gap: 1, alignItems: 'center', height: 30, marginTop: 14, marginBottom: 14, padding: '4px 0' }}>
                        {Array.from({ length: 64 }).map((_, j) => {
                          const phase = j * 0.4 + i;
                          const h = 0.2 + 0.6 * Math.abs(Math.sin(phase) + 0.3 * Math.sin(phase * 2.1));
                          return <div key={j} style={{ flex: 1, height: `${Math.round(h * 100)}%`, background: `oklch(0.5 0.12 ${s.hue})`, borderRadius: 1, opacity: 0.55 }}/>;
                        })}
                      </div>

                      {/* Stats row */}
                      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 0, borderTop: `1px solid ${sm.rule}`, borderBottom: `1px solid ${sm.rule}` }}>
                        {[
                          { k: 'Sessions', v: s.sessions },
                          { k: 'Sim',      v: sim.toFixed(2), c: sim > 0.85 ? sm.green : sim > 0.78 ? sm.cyan : sm.amber },
                          { k: 'Heard',    v: lastHeard },
                          { k: 'Source',   v: 'EMA · 12 blends' },
                        ].map((stat, k) => (
                          <div key={k} style={{ padding: '10px 4px', borderLeft: k ? `1px solid ${sm.rule}` : 'none' }}>
                            <div style={{ fontFamily: sm.mono, fontSize: 9.5, color: sm.textFaint, letterSpacing: '0.12em', textTransform: 'uppercase', marginBottom: 4 }}>{stat.k}</div>
                            <div style={{ fontFamily: sm.mono, fontSize: 12.5, color: stat.c || sm.text }}>{stat.v}</div>
                          </div>
                        ))}
                      </div>

                      {/* Actions */}
                      <div style={{ display: 'flex', gap: 6, marginTop: 14 }}>
                        <button style={{ background: sm.bgRaised, border: `1px solid ${sm.rule}`, color: sm.text, padding: '6px 11px', borderRadius: 5, fontSize: 11.5, fontFamily: sm.sans, display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer' }}>
                          <SMIcon name="play" size={10}/> Sample
                        </button>
                        <button style={{ background: sm.bgRaised, border: `1px solid ${sm.rule}`, color: sm.text, padding: '6px 11px', borderRadius: 5, fontSize: 11.5, fontFamily: sm.sans, cursor: 'pointer' }}>Bind Discord ID</button>
                        <button style={{ background: sm.bgRaised, border: `1px solid ${sm.rule}`, color: sm.text, padding: '6px 11px', borderRadius: 5, fontSize: 11.5, fontFamily: sm.sans, cursor: 'pointer' }}>Re-enroll</button>
                        <div style={{ flex: 1 }}/>
                        <button style={{ background: 'transparent', border: 'none', color: sm.rose, padding: '6px 4px', fontSize: 11.5, fontFamily: sm.sans, cursor: 'pointer' }}>Remove</button>
                      </div>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>

          {/* New profile prompt */}
          <div style={{ marginTop: 24, padding: '20px 24px', border: `1px dashed ${sm.ruleStrong}`, display: 'flex', alignItems: 'center', gap: 18 }}>
            <div style={{ width: 48, height: 48, borderRadius: '50%', border: `1px dashed ${sm.ruleStrong}`, display: 'flex', alignItems: 'center', justifyContent: 'center', color: sm.textDim }}>
              <SMIcon name="plus" size={20} color={sm.textDim}/>
            </div>
            <div style={{ flex: 1 }}>
              <div style={{ fontFamily: sm.serif, fontStyle: 'italic', fontSize: 18, color: sm.text }}>Enroll another voice</div>
              <div style={{ fontSize: 12.5, color: sm.textDim, marginTop: 4 }}>Drag in a clean clip (10–60 seconds, single speaker) or run an enrollment pass on the last recording.</div>
            </div>
            <button style={{ background: sm.bgRaised, border: `1px solid ${sm.ruleStrong}`, color: sm.text, padding: '8px 14px', borderRadius: 6, fontSize: 12.5, fontFamily: sm.sans, cursor: 'pointer' }}>Browse audio</button>
            <button style={{ background: sm.bgRaised, border: `1px solid ${sm.ruleStrong}`, color: sm.text, padding: '8px 14px', borderRadius: 6, fontSize: 12.5, fontFamily: sm.sans, cursor: 'pointer' }}>From last recording</button>
          </div>
        </div>
      </div>
    </div>
  );
};

window.StudioRecordings = StudioRecordings;
window.StudioSpeakers = StudioSpeakers;
