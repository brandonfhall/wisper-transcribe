// Studio — Workshop's structure (sidebar nav, dense data, mono for technical
// fields, terminal log feeds) with Codex's content typography (Newsreader
// serif italic titles, hairline rules, paper-cream on near-black, drop caps
// and pull quotes for reading sections).

const studioTokens = {
  bg: '#0b0f17',              // near-black w/ a hint of navy
  bgRaised: '#11161f',
  bgRaised2: '#161c26',
  bgSunken: '#080b12',
  rule: 'rgba(243, 234, 216, 0.09)',
  ruleStrong: 'rgba(243, 234, 216, 0.18)',
  text: '#f3ead8',            // paper-cream from Codex
  textBright: '#fff8e8',
  textDim: '#a3a89e',
  textFaint: '#5f6571',
  cyan: '#5fd4e7',
  cyanDeep: '#2a8da0',
  green: '#7bd88f',
  amber: '#e4b572',
  rose: '#e88b8b',
  violet: '#a78bfa',
  serif: '"Newsreader", "Instrument Serif", Georgia, serif',
  sans: '"Geist", -apple-system, system-ui, sans-serif',
  mono: '"JetBrains Mono", ui-monospace, monospace',
};

const st = studioTokens;
const SIconBase = window.CodexIcon;
const SIcon = (props) => <SIconBase {...props}/>;

// --- Sidebar ---
const StudioSidebar = ({ active, onNavigate }) => {
  const items = [
    { name: 'Dashboard',   icon: 'sliders' },
    { name: 'Transcribe',  icon: 'upload' },
    { name: 'Record',      icon: 'record' },
    { name: 'Transcripts', icon: 'scroll' },
    { name: 'Speakers',    icon: 'users' },
    { name: 'Campaigns',   icon: 'dice' },
    { name: 'Config',      icon: 'settings' },
  ];
  const go = (name) => (e) => { if (onNavigate) { e.preventDefault(); onNavigate(name); } };
  return (
    <div style={{
      width: 204, background: st.bgSunken, borderRight: `1px solid ${st.rule}`,
      padding: '20px 14px 14px', display: 'flex', flexDirection: 'column', flexShrink: 0,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 11, padding: '0 6px 22px', borderBottom: `1px solid ${st.rule}`, marginBottom: 16, cursor: onNavigate ? 'pointer' : 'default' }} onClick={onNavigate ? () => onNavigate('Dashboard') : undefined}>
        <img src="assets/logo.png" width="30" height="30" style={{ borderRadius: 7 }}/>
        <div>
          <div style={{ fontFamily: st.serif, fontStyle: 'italic', fontSize: 18, fontWeight: 400, color: st.text, letterSpacing: '-0.01em', lineHeight: 1 }}>wisper</div>
          <div style={{ fontFamily: st.mono, fontSize: 9, color: st.textFaint, letterSpacing: '0.12em', marginTop: 3 }}>v0.7.2</div>
        </div>
      </div>

      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 1 }}>
        {items.map((item) => (
          <a key={item.name} href="#" onClick={go(item.name)} style={{
            display: 'flex', alignItems: 'center', gap: 11,
            padding: '8px 10px', borderRadius: 6, fontSize: 13,
            color: active === item.name ? st.text : st.textDim,
            background: active === item.name ? st.bgRaised : 'transparent',
            fontWeight: active === item.name ? 500 : 400, cursor: 'pointer',
            borderLeft: active === item.name ? `2px solid ${st.cyan}` : '2px solid transparent',
            paddingLeft: active === item.name ? 8 : 10,
            textDecoration: 'none',
          }}>
            <SIcon name={item.icon} size={14} color={active === item.name ? st.cyan : st.textDim}/>
            {item.name}
          </a>
        ))}
      </div>

      {/* System footer — Workshop-style */}
      <div style={{ padding: '12px 10px', borderTop: `1px solid ${st.rule}`, fontFamily: st.mono, fontSize: 10, color: st.textFaint, lineHeight: 1.8 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between' }}><span>GPU</span><span style={{ color: st.green }}>● 4070</span></div>
        <div style={{ display: 'flex', justifyContent: 'space-between' }}><span>VRAM</span><span style={{ color: st.text }}>4.2/12</span></div>
        <div style={{ display: 'flex', justifyContent: 'space-between' }}><span>JOBS</span><span style={{ color: st.cyan }}>2 active</span></div>
      </div>
      <div style={{
        display: 'flex', alignItems: 'center', gap: 10, padding: '10px 8px 0',
        borderTop: `1px solid ${st.rule}`, marginTop: 10,
      }}>
        <div style={{ width: 28, height: 28, borderRadius: 7, background: st.bgRaised, color: st.text, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 11, fontWeight: 500, fontFamily: st.serif, fontStyle: 'italic', border: `1px solid ${st.rule}` }}>B</div>
        <div>
          <div style={{ fontSize: 12, color: st.text, lineHeight: 1 }}>brandonh</div>
          <div style={{ fontSize: 10, color: st.textFaint, fontFamily: st.mono, marginTop: 2 }}>localhost:8080</div>
        </div>
      </div>
    </div>
  );
};

// --- Toolbar ---
const StudioToolbar = ({ kicker, title, sub, actions, hideSearch }) => (
  <div style={{
    display: 'flex', alignItems: 'center', justifyContent: 'space-between',
    padding: '14px 24px', borderBottom: `1px solid ${st.rule}`, background: st.bg, flexShrink: 0,
  }}>
    <div style={{ display: 'flex', alignItems: 'baseline', gap: 16 }}>
      <div>
        {kicker && <div style={{ fontFamily: st.mono, fontSize: 10, color: st.textFaint, letterSpacing: '0.15em', textTransform: 'uppercase', marginBottom: 2 }}>{kicker}</div>}
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 12 }}>
          <div style={{ fontFamily: st.serif, fontStyle: 'italic', fontSize: 22, fontWeight: 400, color: st.text, letterSpacing: '-0.01em' }}>{title}</div>
          {sub && <div style={{ fontFamily: st.mono, fontSize: 11, color: st.textFaint, letterSpacing: '0.06em' }}>{sub}</div>}
        </div>
      </div>
    </div>
    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
      {!hideSearch && (
        <div style={{
          display: 'flex', alignItems: 'center', gap: 8, padding: '7px 11px',
          background: st.bgRaised, border: `1px solid ${st.rule}`, borderRadius: 6,
          width: 240, fontSize: 12, color: st.textFaint,
        }}>
          <SIcon name="search" size={12} color={st.textFaint}/>
          <span>Search transcripts, speakers …</span>
          <span style={{ marginLeft: 'auto', fontFamily: st.mono, fontSize: 10, color: st.textFaint, padding: '1px 6px', border: `1px solid ${st.rule}`, borderRadius: 3 }}>⌘K</span>
        </div>
      )}
      {actions}
    </div>
  </div>
);

// --- Section header (Codex pattern) ---
const StudioSectionHead = ({ kicker, children, action }) => (
  <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', marginBottom: 14 }}>
    <div style={{ display: 'flex', alignItems: 'baseline', gap: 14 }}>
      {kicker && <div style={{ fontFamily: st.mono, fontSize: 10, color: st.textFaint, letterSpacing: '0.18em', textTransform: 'uppercase' }}>{kicker}</div>}
      <h2 style={{ fontFamily: st.serif, fontStyle: 'italic', fontWeight: 400, fontSize: 20, color: st.text, margin: 0, letterSpacing: '-0.01em' }}>{children}</h2>
    </div>
    {action}
  </div>
);

// --- Dashboard ---
const StudioDashboard = ({ onNavigate }) => {
  const d = window.WISPER_DATA;
  return (
    <div data-screen-label="Studio · Dashboard" style={{
      width: '100%', height: '100%', background: st.bg, color: st.text,
      fontFamily: st.sans, overflow: 'hidden', display: 'flex',
    }}>
      <StudioSidebar active="Dashboard" onNavigate={onNavigate}/>
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        <StudioToolbar
          kicker="Dashboard"
          title="Tuesday evening"
          sub="APR 28 · 21:42:18"
          actions={
            <>
              <button onClick={() => onNavigate && onNavigate('Transcribe')} style={{ background: st.bgRaised, border: `1px solid ${st.rule}`, color: st.text, padding: '7px 13px', borderRadius: 6, fontSize: 12.5, fontFamily: st.sans, display: 'flex', alignItems: 'center', gap: 7, cursor: 'pointer' }}>
                <SIcon name="upload" size={12}/> Transcribe
              </button>
              <button onClick={() => onNavigate && onNavigate('Record')} style={{ background: st.cyan, color: st.bg, border: 'none', padding: '7px 14px', borderRadius: 6, fontSize: 12.5, fontFamily: st.sans, fontWeight: 600, display: 'flex', alignItems: 'center', gap: 7, cursor: 'pointer', boxShadow: `0 0 20px ${st.cyan}30` }}>
                <SIcon name="record" size={10} color={st.bg}/> Start session
              </button>
            </>
          }
        />

        <div style={{ flex: 1, padding: '24px 28px 28px', overflow: 'auto' }}>
          {/* Editorial intro */}
          <div style={{ paddingBottom: 24, borderBottom: `1px solid ${st.rule}`, marginBottom: 24 }}>
            <h1 style={{ fontFamily: st.serif, fontWeight: 300, fontSize: 38, color: st.text, margin: 0, lineHeight: 1.05, letterSpacing: '-0.025em', maxWidth: 720 }}>
              Two sessions are processing.
              <span style={{ color: st.textDim, fontStyle: 'italic' }}> Theo is still missing.</span>
            </h1>
          </div>

          {/* Stats — Codex hairlines on Workshop density */}
          <div style={{
            display: 'grid', gridTemplateColumns: 'repeat(6, 1fr)',
            borderTop: `1px solid ${st.rule}`, borderBottom: `1px solid ${st.rule}`,
            marginBottom: 24,
          }}>
            {[
              { k: 'In progress', v: '2',     s: '1 queued',    indicator: true, c: st.cyan },
              { k: 'Archive',     v: '38',    s: 'transcripts' },
              { k: 'Hours',       v: '142',   s: 'captured' },
              { k: 'Voices',      v: '6',     s: 'enrolled' },
              { k: 'Words',       v: '1.18M', s: 'all-time' },
              { k: 'Campaigns',   v: '3',     s: 'active' },
            ].map((s, i) => (
              <div key={i} style={{ padding: '18px 18px', borderLeft: i ? `1px solid ${st.rule}` : 'none' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 7, marginBottom: 10 }}>
                  {s.indicator && <span style={{ width: 5, height: 5, borderRadius: '50%', background: st.cyan, boxShadow: `0 0 6px ${st.cyan}` }}/>}
                  <div style={{ fontFamily: st.mono, fontSize: 10, color: st.textFaint, letterSpacing: '0.15em', textTransform: 'uppercase' }}>{s.k}</div>
                </div>
                <div style={{ fontFamily: st.serif, fontSize: 32, fontWeight: 300, color: s.c || st.text, lineHeight: 1, letterSpacing: '-0.02em' }}>{s.v}</div>
                <div style={{ fontSize: 11.5, color: st.textFaint, marginTop: 6 }}>{s.s}</div>
              </div>
            ))}
          </div>

          {/* Three-column body */}
          <div style={{ display: 'grid', gridTemplateColumns: '1.6fr 1fr', gap: 36 }}>
            {/* Left: jobs + archive */}
            <div>
              <StudioSectionHead kicker="Now processing" action={<a onClick={() => onNavigate && onNavigate('Transcribe')} style={{ fontFamily: st.mono, fontSize: 11, color: st.textDim, cursor: 'pointer' }}>view queue →</a>}>
                The desk
              </StudioSectionHead>

              {/* Job table — Workshop grid + Codex serif titles */}
              <div style={{
                display: 'grid', gridTemplateColumns: '12px 1.7fr 110px 1fr 70px 56px',
                gap: 14, padding: '8px 0', borderTop: `1px solid ${st.rule}`, borderBottom: `1px solid ${st.rule}`,
                fontFamily: st.mono, fontSize: 10, color: st.textFaint, letterSpacing: '0.12em',
              }}>
                <div></div><div>SESSION</div><div>CAMPAIGN</div><div>STAGE</div><div style={{ textAlign: 'right' }}>ETA</div><div style={{ textAlign: 'right' }}>%</div>
              </div>

              {d.jobs.map((job, i) => {
                const camp = d.campaigns.find(c => c.slug === job.campaign);
                const active = job.progress > 0 && job.progress < 1;
                return (
                  <div key={job.id} style={{
                    display: 'grid', gridTemplateColumns: '12px 1.7fr 110px 1fr 70px 56px',
                    gap: 14, padding: '14px 0', borderBottom: `1px solid ${st.rule}`,
                    alignItems: 'center',
                  }}>
                    <div>
                      {active ?
                        <span style={{ display: 'block', width: 8, height: 8, borderRadius: '50%', background: st.cyan, boxShadow: `0 0 6px ${st.cyan}` }}/> :
                        <span style={{ display: 'block', width: 8, height: 8, borderRadius: '50%', border: `1px solid ${st.textFaint}` }}/>}
                    </div>
                    <div>
                      <div style={{ fontFamily: st.serif, fontSize: 15, color: st.text }}>{job.title.replace(/\.mp3$/, '')}</div>
                      <div style={{ fontFamily: st.mono, fontSize: 10, color: st.textFaint, marginTop: 3 }}>{job.id} · started {job.startedAt}</div>
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: st.textDim }}>
                      <span style={{ width: 5, height: 5, borderRadius: '50%', background: camp?.color }}/>
                      {camp?.name?.split(' ')[0]}
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                      <div style={{ flex: 1, height: 2, background: st.bgSunken, borderRadius: 1, overflow: 'hidden' }}>
                        <div style={{ width: `${job.progress * 100}%`, height: '100%', background: active ? st.cyan : 'transparent', boxShadow: active ? `0 0 6px ${st.cyan}80` : 'none' }}/>
                      </div>
                      <span style={{ fontFamily: st.mono, fontSize: 10.5, color: st.textDim, minWidth: 64 }}>{job.stage}</span>
                    </div>
                    <div style={{ fontFamily: st.mono, fontSize: 11, color: st.textDim, textAlign: 'right' }}>{job.eta}</div>
                    <div style={{ fontFamily: st.mono, fontSize: 12, color: active ? st.cyan : st.textFaint, textAlign: 'right', fontWeight: 500 }}>
                      {job.progress > 0 ? Math.round(job.progress * 100) : '—'}
                    </div>
                  </div>
                );
              })}

              {/* Mini log strip — Workshop terminal energy */}
              <div style={{ background: st.bgSunken, padding: '12px 14px', border: `1px solid ${st.rule}`, borderTop: 'none', fontFamily: st.mono, fontSize: 11, color: st.textDim, lineHeight: 1.75, marginBottom: 36 }}>
                <div><span style={{ color: st.textFaint }}>21:51:44</span> <span style={{ color: st.cyan }}>j_8a1f</span> diarize 62% · 02:34:11 / 04:08:00</div>
                <div><span style={{ color: st.textFaint }}>21:51:42</span> <span style={{ color: st.cyan }}>j_8a1f</span> emb_extract speaker_03 (0.84 sim)</div>
                <div><span style={{ color: st.textFaint }}>21:51:40</span> <span style={{ color: st.amber }}>j_8a20</span> refine vocab_pass 3/5 · 14 corrections</div>
              </div>

              {/* Archive — Codex editorial list */}
              <StudioSectionHead kicker="Recent" action={<a onClick={() => onNavigate && onNavigate('Transcripts')} style={{ fontFamily: st.mono, fontSize: 11, color: st.textDim, cursor: 'pointer' }}>archive →</a>}>
                The archive
              </StudioSectionHead>
              <div>
                {d.completed.slice(0, 6).map((t, i) => {
                  const camp = d.campaigns.find(c => c.slug === t.campaign);
                  return (
                    <div key={i} onClick={() => onNavigate && onNavigate('TranscriptDetail')} style={{
                      display: 'grid', gridTemplateColumns: '1fr 160px 70px 80px 60px',
                      gap: 18, alignItems: 'baseline', padding: '14px 0',
                      borderTop: i === 0 ? `1px solid ${st.rule}` : 'none',
                      borderBottom: `1px solid ${st.rule}`, cursor: 'pointer',
                    }}>
                      <div>
                        <div style={{ fontFamily: st.serif, fontSize: 16, color: st.text }}>{t.name}</div>
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 7, fontSize: 11.5, color: st.textDim }}>
                        <span style={{ width: 5, height: 5, borderRadius: '50%', background: camp?.color }}/>
                        {camp?.name}
                      </div>
                      <div style={{ fontFamily: st.mono, fontSize: 11, color: st.textDim, textAlign: 'right' }}>{t.duration}</div>
                      <div style={{ fontFamily: st.mono, fontSize: 11, color: st.textFaint, textAlign: 'right' }}>{t.words.toLocaleString()}w</div>
                      <div style={{ fontFamily: st.mono, fontSize: 11, color: st.textFaint, textAlign: 'right' }}>{t.date}</div>
                    </div>
                  );
                })}
              </div>
            </div>

            {/* Right column */}
            <div>
              <StudioSectionHead kicker="Campaigns" action={<a onClick={() => onNavigate && onNavigate('Campaigns')} style={{ fontFamily: st.mono, fontSize: 11, color: st.cyan, cursor: 'pointer' }}>+ NEW</a>}>
                The tables
              </StudioSectionHead>
              <div style={{ borderTop: `1px solid ${st.rule}`, marginBottom: 36 }}>
                {d.campaigns.map((c) => (
                  <div key={c.slug} onClick={() => onNavigate && onNavigate('Campaigns')} style={{ padding: '15px 0', borderBottom: `1px solid ${st.rule}`, display: 'flex', alignItems: 'center', gap: 14, cursor: 'pointer' }}>
                    <div style={{ width: 3, height: 34, background: c.color, borderRadius: 1.5 }}/>
                    <div style={{ flex: 1 }}>
                      <div style={{ fontFamily: st.serif, fontSize: 16, color: st.text }}>{c.name}</div>
                      <div style={{ fontFamily: st.mono, fontSize: 10, color: st.textFaint, marginTop: 3, letterSpacing: '0.1em' }}>{c.system.toUpperCase()}</div>
                    </div>
                    <div style={{ textAlign: 'right' }}>
                      <div style={{ fontFamily: st.serif, fontSize: 22, color: st.text, fontWeight: 300, lineHeight: 1 }}>{c.sessions}</div>
                      <div style={{ fontFamily: st.mono, fontSize: 9, color: st.textFaint, letterSpacing: '0.12em', marginTop: 3 }}>SESSIONS</div>
                    </div>
                  </div>
                ))}
              </div>

              <StudioSectionHead kicker="Speakers" action={<a onClick={() => onNavigate && onNavigate('Speakers')} style={{ fontFamily: st.mono, fontSize: 11, color: st.textDim, cursor: 'pointer' }}>manage →</a>}>
                The voices
              </StudioSectionHead>
              <div style={{ borderTop: `1px solid ${st.rule}` }}>
                {d.speakers.slice(0, 5).map((s, i) => (
                  <div key={s.name} onClick={() => onNavigate && onNavigate('Speakers')} style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '11px 0', borderBottom: `1px solid ${st.rule}`, cursor: 'pointer' }}>
                    <div style={{
                      width: 26, height: 26, borderRadius: '50%',
                      background: `oklch(0.5 0.1 ${s.hue})`,
                      color: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'center',
                      fontSize: 10, fontWeight: 600, fontFamily: st.mono,
                    }}>{s.initials}</div>
                    <div style={{ flex: 1 }}>
                      <div style={{ fontFamily: st.serif, fontSize: 14, color: st.text }}>{s.name}</div>
                      <div style={{ fontFamily: st.mono, fontSize: 9.5, color: st.textFaint, letterSpacing: '0.1em', marginTop: 2 }}>{s.role.toUpperCase()}</div>
                    </div>
                    <div style={{ fontFamily: st.mono, fontSize: 11, color: st.textDim }}>{s.sessions} sessions</div>
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

window.studioTokens = studioTokens;
window.StudioSidebar = StudioSidebar;
window.StudioToolbar = StudioToolbar;
window.StudioSectionHead = StudioSectionHead;
window.StudioDashboard = StudioDashboard;
window.SIcon = SIcon;
