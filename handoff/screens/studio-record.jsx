// Studio · Record — live Discord recording screen with per-speaker meters.
// The marquee live screen — what a DM sees during a session.

const sr = window.studioTokens;
const SRIcon = window.SIcon;
const SRSidebar = window.StudioSidebar;
const SRSH = window.StudioSectionHead;

// Per-speaker meter — vertical bars driven by `level` (0-1) with a sine wave
// modulation so it looks alive even at a glance. Cyan when speaking, dim otherwise.
const SpeakerMeter = ({ level, hue, isSpeaking, bars = 28 }) => {
  // deterministic-ish wave so it renders identically every paint
  return (
    <div style={{ display: 'flex', alignItems: 'flex-end', gap: 2, height: 36, flex: 1 }}>
      {Array.from({ length: bars }).map((_, i) => {
        const phase = (i / bars) * Math.PI * 2;
        // shape: peak in middle of bar set, modulated by overall level
        const base = 0.25 + 0.45 * Math.abs(Math.sin(phase * 1.3 + i * 0.4));
        const h = isSpeaking ? Math.max(0.08, base * (0.55 + level * 0.7)) : Math.max(0.04, 0.18 * (0.3 + level));
        return (
          <div key={i} style={{
            flex: 1,
            height: `${Math.round(h * 100)}%`,
            background: isSpeaking
              ? `linear-gradient(180deg, ${sr.cyan}, ${sr.cyan}55)`
              : sr.ruleStrong,
            boxShadow: isSpeaking && h > 0.5 ? `0 0 6px ${sr.cyan}80` : 'none',
            borderRadius: 1,
            transition: 'height 100ms ease-out',
          }}/>
        );
      })}
    </div>
  );
};

const StudioRecord = ({ onNavigate }) => {
  const d = window.WISPER_DATA;
  const l = d.live;
  return (
    <div data-screen-label="Studio · Record" style={{
      width: '100%', height: '100%', background: sr.bg, color: sr.text,
      fontFamily: sr.sans, overflow: 'hidden', display: 'flex',
    }}>
      <SRSidebar active="Record" onNavigate={onNavigate}/>
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>

        {/* Toolbar — recording-state-aware */}
        <div style={{
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          padding: '14px 24px', borderBottom: `1px solid ${sr.rule}`,
          background: `linear-gradient(180deg, ${sr.rose}10 0%, transparent 100%)`, flexShrink: 0,
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 18 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <span style={{
                width: 12, height: 12, borderRadius: '50%', background: sr.rose,
                boxShadow: `0 0 12px ${sr.rose}, 0 0 24px ${sr.rose}60`,
              }}/>
              <div style={{ fontFamily: sr.mono, fontSize: 10, color: sr.rose, letterSpacing: '0.18em', fontWeight: 600 }}>RECORDING</div>
            </div>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
              <div style={{ fontFamily: sr.mono, fontSize: 26, color: sr.text, fontWeight: 500, letterSpacing: '-0.01em' }}>{l.elapsed}</div>
              <div style={{ fontFamily: sr.mono, fontSize: 11, color: sr.textFaint }}>elapsed</div>
            </div>
            <div style={{ width: 1, height: 28, background: sr.rule }}/>
            <div>
              <div style={{ fontFamily: sr.mono, fontSize: 10, color: sr.textFaint, letterSpacing: '0.15em', textTransform: 'uppercase' }}>Channel</div>
              <div style={{ fontFamily: sr.serif, fontStyle: 'italic', fontSize: 16, color: sr.text, marginTop: 2 }}>{l.channel}</div>
            </div>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <button style={{ background: sr.bgRaised, border: `1px solid ${sr.rule}`, color: sr.text, padding: '7px 13px', borderRadius: 6, fontSize: 12.5, fontFamily: sr.sans, display: 'flex', alignItems: 'center', gap: 7, cursor: 'pointer' }}>
              <SRIcon name="spark" size={12}/> Add marker
            </button>
            <button style={{ background: sr.bgRaised, border: `1px solid ${sr.rule}`, color: sr.text, padding: '7px 13px', borderRadius: 6, fontSize: 12.5, fontFamily: sr.sans, cursor: 'pointer' }}>
              Pause
            </button>
            <button style={{ background: sr.rose, color: '#1a0a0a', border: 'none', padding: '7px 14px', borderRadius: 6, fontSize: 12.5, fontFamily: sr.sans, fontWeight: 600, display: 'flex', alignItems: 'center', gap: 7, cursor: 'pointer', boxShadow: `0 0 16px ${sr.rose}50` }}>
              <span style={{ width: 9, height: 9, background: '#1a0a0a' }}/> Stop recording
            </button>
          </div>
        </div>

        <div style={{ flex: 1, padding: '20px 28px 20px', overflow: 'auto', display: 'grid', gridTemplateColumns: '1fr 320px', gap: 28, alignItems: 'start' }}>
          {/* Main column — speakers + ticker */}
          <div>
            {/* Status strip */}
            <div style={{
              display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)',
              borderTop: `1px solid ${sr.rule}`, borderBottom: `1px solid ${sr.rule}`,
              marginBottom: 28,
            }}>
              {[
                { k: 'Speakers',  v: '5',     s: '5 of 5 mapped',  color: sr.cyan },
                { k: 'Segments',  v: '84',    s: '60s rotation' },
                { k: 'Bot ping',  v: '18 ms', s: 'JDA · stable',   color: sr.green },
                { k: 'Storage',   v: '247 MB', s: 'Ogg/Opus · ./recordings' },
                { k: 'Markers',   v: '3',     s: 'last 02:13:48' },
              ].map((s, i) => (
                <div key={i} style={{ padding: '14px 16px', borderLeft: i ? `1px solid ${sr.rule}` : 'none' }}>
                  <div style={{ fontFamily: sr.mono, fontSize: 10, color: sr.textFaint, letterSpacing: '0.15em', textTransform: 'uppercase', marginBottom: 6 }}>{s.k}</div>
                  <div style={{ fontFamily: sr.serif, fontSize: 22, color: s.color || sr.text, fontWeight: 300, lineHeight: 1 }}>{s.v}</div>
                  <div style={{ fontSize: 11, color: sr.textFaint, marginTop: 5 }}>{s.s}</div>
                </div>
              ))}
            </div>

            {/* Hero: per-speaker meters */}
            <SRSH kicker="Voices · live" action={<div style={{ display: 'flex', alignItems: 'center', gap: 8, fontFamily: sr.mono, fontSize: 11, color: sr.textFaint, letterSpacing: '0.1em' }}>
              <span style={{ width: 6, height: 6, borderRadius: '50%', background: sr.green, boxShadow: `0 0 6px ${sr.green}` }}/>
              CAPTURING · 48 kHz STEREO → 16 kHz MONO
            </div>}>
              At the table
            </SRSH>

            <div style={{ borderTop: `1px solid ${sr.rule}` }}>
              {l.levels.map((s) => {
                const profile = d.speakers.find(p => p.name === s.name);
                return (
                  <div key={s.name} style={{
                    display: 'grid', gridTemplateColumns: '38px 1fr 1fr auto 110px',
                    gap: 18, alignItems: 'center', padding: '18px 0',
                    borderBottom: `1px solid ${sr.rule}`,
                    background: s.isSpeaking ? `linear-gradient(90deg, ${sr.cyan}06, transparent 60%)` : 'transparent',
                  }}>
                    <div style={{
                      width: 36, height: 36, borderRadius: '50%',
                      background: `oklch(0.5 0.12 ${s.hue})`,
                      color: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'center',
                      fontFamily: sr.mono, fontSize: 13, fontWeight: 600,
                      boxShadow: s.isSpeaking ? `0 0 0 2px ${sr.cyan}80, 0 0 14px ${sr.cyan}40` : 'none',
                      transition: 'box-shadow 200ms',
                    }}>{s.name[0]}</div>
                    <div>
                      <div style={{ fontFamily: sr.serif, fontSize: 18, color: sr.text }}>{s.name}</div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 3 }}>
                        <div style={{ fontFamily: sr.mono, fontSize: 10, color: sr.textFaint, letterSpacing: '0.1em' }}>{profile?.role.toUpperCase()}</div>
                        <div style={{ fontFamily: sr.mono, fontSize: 10, color: sr.textFaint }}>· discord_id #{(341278901 + l.levels.indexOf(s) * 137).toString().slice(0, 8)}…</div>
                      </div>
                    </div>
                    <SpeakerMeter level={s.level} hue={s.hue} isSpeaking={s.isSpeaking}/>
                    <div style={{ minWidth: 60, textAlign: 'right' }}>
                      <div style={{ fontFamily: sr.mono, fontSize: 13, color: s.isSpeaking ? sr.cyan : sr.textDim, fontWeight: 500 }}>{[31, 12, 47, 18, 11][l.levels.indexOf(s)]}m</div>
                      <div style={{ fontFamily: sr.mono, fontSize: 9.5, color: sr.textFaint, letterSpacing: '0.1em', marginTop: 3 }}>SPEAKING</div>
                    </div>
                    <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
                      {s.isSpeaking ? (
                        <div style={{
                          padding: '4px 10px', borderRadius: 999,
                          background: sr.cyan + '18', border: `1px solid ${sr.cyan}50`,
                          color: sr.cyan, fontFamily: sr.mono, fontSize: 10, fontWeight: 600, letterSpacing: '0.14em',
                          display: 'flex', alignItems: 'center', gap: 6,
                        }}>
                          <span style={{ width: 6, height: 6, borderRadius: '50%', background: sr.cyan, boxShadow: `0 0 6px ${sr.cyan}` }}/>
                          LIVE
                        </div>
                      ) : (
                        <div style={{
                          padding: '4px 10px', borderRadius: 999,
                          border: `1px solid ${sr.rule}`,
                          color: sr.textFaint, fontFamily: sr.mono, fontSize: 10, letterSpacing: '0.14em',
                        }}>QUIET</div>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>

            {/* Live ticker */}
            <div style={{ marginTop: 28 }}>
              <SRSH kicker="Heard so far" action={<a style={{ fontFamily: sr.mono, fontSize: 11, color: sr.textDim }}>auto-scroll: ON ▾</a>}>
                The thread
              </SRSH>
              <div style={{ background: sr.bgRaised, border: `1px solid ${sr.rule}`, padding: '14px 18px', borderRadius: 4 }}>
                {[
                  { t: '01:24:09', sp: 'Priya',  hue: 320, text: '… I want to look behind the curtain. Is there anyone there?', live: true },
                  { t: '01:24:02', sp: 'Alice',  hue: 200, text: 'Give me a Spot Hidden, Priya. And anyone else who said they\'re watching her.' },
                  { t: '01:23:54', sp: 'Yuki',   hue: 60,  text: 'I\'m watching Priya. I think she\'s about to do something stupid.' },
                  { t: '01:23:48', sp: 'Marcus', hue: 30,  text: 'For the record I think this is a terrible idea.' },
                  { t: '01:23:41', sp: 'Priya',  hue: 320, text: 'The curtain moved on its own. The window\'s closed.' },
                  { t: '01:23:35', sp: 'Devon',  hue: 140, text: 'Anyone else feel that draft?' },
                ].map((line, i) => (
                  <div key={i} style={{
                    display: 'grid', gridTemplateColumns: '60px 90px 1fr',
                    gap: 14, padding: '6px 0', alignItems: 'baseline',
                    opacity: i === 0 ? 1 : Math.max(0.45, 1 - i * 0.12),
                  }}>
                    <span style={{ fontFamily: sr.mono, fontSize: 10.5, color: sr.textFaint }}>{line.t}</span>
                    <span style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
                      <span style={{ width: 6, height: 6, borderRadius: '50%', background: `oklch(0.6 0.14 ${line.hue})`, boxShadow: line.live ? `0 0 6px oklch(0.6 0.14 ${line.hue})` : 'none' }}/>
                      <span style={{ fontFamily: sr.serif, fontSize: 13, color: sr.text }}>{line.sp}</span>
                    </span>
                    <span style={{ fontSize: 13.5, color: i === 0 ? sr.text : sr.textDim, lineHeight: 1.5 }}>{line.text}</span>
                  </div>
                ))}
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 0 4px', borderTop: `1px solid ${sr.rule}`, marginTop: 10, fontFamily: sr.mono, fontSize: 10.5, color: sr.textFaint, letterSpacing: '0.1em' }}>
                  <span style={{ width: 6, height: 6, borderRadius: '50%', background: sr.cyan, boxShadow: `0 0 6px ${sr.cyan}` }}/>
                  PARTIAL TRANSCRIPT · WHISPER-STREAMING · 3s WINDOW
                </div>
              </div>
            </div>
          </div>

          {/* Sidebar */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
            {/* Session header */}
            <div>
              <div style={{ fontFamily: sr.mono, fontSize: 10, color: sr.textFaint, letterSpacing: '0.18em', textTransform: 'uppercase', marginBottom: 8 }}>This session</div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
                <span style={{ width: 8, height: 8, borderRadius: '50%', background: d.campaigns[0].color }}/>
                <span style={{ fontFamily: sr.serif, fontStyle: 'italic', fontSize: 18, color: sr.text }}>{d.campaigns[0].name}</span>
              </div>
              <div style={{ fontFamily: sr.mono, fontSize: 11, color: sr.textDim, lineHeight: 1.7 }}>
                <div>S1 E7 · The Yellow Throne</div>
                <div>started apr 28 · 20:18</div>
                <div>recording_id <span style={{ color: sr.cyan }}>r_a8f1c2…</span></div>
              </div>
            </div>

            {/* Segment manifest */}
            <div>
              <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginBottom: 10 }}>
                <div style={{ fontFamily: sr.mono, fontSize: 10, color: sr.textFaint, letterSpacing: '0.18em', textTransform: 'uppercase' }}>Segment manifest</div>
                <div style={{ flex: 1, height: 1, background: sr.rule }}/>
                <div style={{ fontFamily: sr.mono, fontSize: 10, color: sr.green }}>● HEALTHY</div>
              </div>
              <div style={{ borderTop: `1px solid ${sr.rule}` }}>
                {[
                  { n: '#084', t: 'writing', size: '— KB',   age: 'now',   active: true },
                  { n: '#083', t: 'sealed',  size: '5.2 MB', age: '46s' },
                  { n: '#082', t: 'sealed',  size: '5.1 MB', age: '1m 46s' },
                  { n: '#081', t: 'sealed',  size: '5.3 MB', age: '2m 46s' },
                  { n: '#080', t: 'sealed',  size: '5.0 MB', age: '3m 46s' },
                ].map((s, i) => (
                  <div key={s.n} style={{ display: 'grid', gridTemplateColumns: 'auto 1fr auto', gap: 12, padding: '8px 0', borderBottom: `1px solid ${sr.rule}`, alignItems: 'center', fontFamily: sr.mono, fontSize: 11 }}>
                    <span style={{ color: s.active ? sr.cyan : sr.textFaint, fontWeight: s.active ? 600 : 400 }}>{s.n}</span>
                    <span style={{ color: s.active ? sr.cyan : sr.textDim, fontSize: 10.5, letterSpacing: '0.06em' }}>{s.t}</span>
                    <span style={{ color: sr.textFaint, fontSize: 10.5 }}>{s.size} · {s.age}</span>
                  </div>
                ))}
              </div>
              <div style={{ fontFamily: sr.mono, fontSize: 10, color: sr.textFaint, marginTop: 8, lineHeight: 1.7 }}>
                <div>codec: Ogg/Opus · 48 kHz · 64 kbps</div>
                <div>rotation: 60s · packet-count crash-safe</div>
              </div>
            </div>

            {/* Markers */}
            <div>
              <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginBottom: 10 }}>
                <div style={{ fontFamily: sr.mono, fontSize: 10, color: sr.textFaint, letterSpacing: '0.18em', textTransform: 'uppercase' }}>Markers</div>
                <div style={{ flex: 1, height: 1, background: sr.rule }}/>
              </div>
              <div style={{ borderTop: `1px solid ${sr.rule}` }}>
                {[
                  { t: '02:13:48', label: 'Yellow coat man' },
                  { t: '01:42:12', label: 'Theo disconnected' },
                  { t: '00:14:08', label: 'Session start' },
                ].map((m) => (
                  <div key={m.t} style={{ padding: '10px 0', borderBottom: `1px solid ${sr.rule}`, display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 12 }}>
                    <span style={{ fontFamily: sr.serif, fontSize: 13.5, color: sr.text, fontStyle: 'italic' }}>{m.label}</span>
                    <span style={{ fontFamily: sr.mono, fontSize: 10.5, color: sr.textFaint }}>{m.t}</span>
                  </div>
                ))}
              </div>
            </div>

            {/* What happens when you stop */}
            <div style={{ padding: '14px 16px', background: sr.bgRaised, border: `1px solid ${sr.rule}`, borderRadius: 4 }}>
              <div style={{ fontFamily: sr.mono, fontSize: 10, color: sr.textFaint, letterSpacing: '0.15em', textTransform: 'uppercase', marginBottom: 8 }}>When you stop</div>
              <ol style={{ margin: 0, padding: '0 0 0 18px', fontSize: 12, color: sr.textDim, lineHeight: 1.6 }}>
                <li>Final segment sealed</li>
                <li>Recording saved to <span style={{ fontFamily: sr.mono, color: sr.text }}>./recordings/r_a8f1c2…</span></li>
                <li>Transcription queued automatically</li>
              </ol>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

window.StudioRecord = StudioRecord;
