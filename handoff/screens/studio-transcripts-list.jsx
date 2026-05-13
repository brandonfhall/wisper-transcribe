// Studio · Transcripts list — grouped by campaign, sortable, filterable.

const sl = window.studioTokens;
const SLIcon = window.SIcon;
const SLSidebar = window.StudioSidebar;
const SLSH = window.StudioSectionHead;
const SLToolbar = window.StudioToolbar;

const StudioTranscriptsList = ({ onNavigate }) => {
  const d = window.WISPER_DATA;
  // Group by campaign
  const grouped = d.campaigns
    .map(c => ({
      campaign: c,
      items: d.completed.filter(t => t.campaign === c.slug),
    }))
    .filter(g => g.items.length > 0);

  return (
    <div data-screen-label="Studio · Transcripts" style={{
      width: '100%', height: '100%', background: sl.bg, color: sl.text,
      fontFamily: sl.sans, overflow: 'hidden', display: 'flex',
    }}>
      <SLSidebar active="Transcripts" onNavigate={onNavigate}/>
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        <SLToolbar kicker="Transcripts" title="The archive" sub={`${d.completed.length} TRANSCRIPTS · ${d.campaigns.length} CAMPAIGNS`}
          actions={
            <button onClick={() => onNavigate && onNavigate('Transcribe')} style={{ background: sl.cyan, color: sl.bg, border: 'none', padding: '7px 14px', borderRadius: 6, fontSize: 12.5, fontFamily: sl.sans, fontWeight: 600, display: 'flex', alignItems: 'center', gap: 7, cursor: 'pointer' }}>
              <SLIcon name="upload" size={11} color={sl.bg}/> New transcription
            </button>
          }
        />

        <div style={{ flex: 1, padding: '24px 28px', overflow: 'auto' }}>
          {/* Filter row */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 4, marginBottom: 24, flexWrap: 'wrap' }}>
            <button style={{
              padding: '5px 12px', borderRadius: 999,
              background: sl.bgRaised, border: `1px solid ${sl.ruleStrong}`,
              color: sl.text, fontSize: 12, fontFamily: sl.sans, cursor: 'pointer',
            }}>All <span style={{ fontFamily: sl.mono, fontSize: 10.5, color: sl.textFaint, marginLeft: 6 }}>{d.completed.length}</span></button>
            {d.campaigns.map((c) => (
              <button key={c.slug} style={{
                padding: '5px 12px', borderRadius: 999, background: 'transparent',
                border: `1px solid ${sl.rule}`, color: sl.textDim, fontSize: 12, fontFamily: sl.sans,
                display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer',
              }}>
                <span style={{ width: 5, height: 5, borderRadius: '50%', background: c.color }}/>
                {c.name}
                <span style={{ fontFamily: sl.mono, fontSize: 10.5, color: sl.textFaint }}>{d.completed.filter(t => t.campaign === c.slug).length}</span>
              </button>
            ))}
            <div style={{ flex: 1 }}/>
            <div style={{
              display: 'flex', alignItems: 'center', gap: 8, padding: '6px 10px',
              background: sl.bgRaised, border: `1px solid ${sl.rule}`, borderRadius: 6,
              width: 240, fontSize: 12, color: sl.textFaint,
            }}>
              <SLIcon name="search" size={11} color={sl.textFaint}/>
              <span>Filter transcripts, NPCs, loot…</span>
            </div>
          </div>

          {/* Groups */}
          {grouped.map((g) => (
            <div key={g.campaign.slug} style={{ marginBottom: 32 }}>
              <div style={{ display: 'flex', alignItems: 'baseline', gap: 14, marginBottom: 12 }}>
                <span style={{ display: 'inline-flex', alignItems: 'center', gap: 9 }}>
                  <span style={{ width: 7, height: 7, borderRadius: '50%', background: g.campaign.color }}/>
                  <span style={{ fontFamily: sl.serif, fontStyle: 'italic', fontSize: 20, color: sl.text, letterSpacing: '-0.01em' }}>{g.campaign.name}</span>
                </span>
                <a onClick={() => onNavigate && onNavigate('Campaigns')} style={{ fontFamily: sl.mono, fontSize: 10.5, color: sl.textFaint, letterSpacing: '0.1em', cursor: 'pointer' }}>{g.campaign.system.toUpperCase()} · {g.items.length} EPISODES</a>
                <div style={{ flex: 1, height: 1, background: sl.rule }}/>
                <a onClick={() => onNavigate && onNavigate('Campaigns')} style={{ fontFamily: sl.mono, fontSize: 11, color: sl.cyan, cursor: 'pointer' }}>open campaign →</a>
              </div>

              {/* Headers */}
              <div style={{
                display: 'grid', gridTemplateColumns: '14px 2fr 80px 80px 80px 110px 60px',
                gap: 16, padding: '8px 16px', borderTop: `1px solid ${sl.rule}`, borderBottom: `1px solid ${sl.rule}`,
                fontFamily: sl.mono, fontSize: 10, color: sl.textFaint, letterSpacing: '0.12em',
              }}>
                <div></div><div>SESSION</div><div style={{ textAlign: 'right' }}>DURATION</div><div style={{ textAlign: 'right' }}>WORDS</div><div style={{ textAlign: 'right' }}>SPEAKERS</div><div>STATE</div><div style={{ textAlign: 'right' }}>DATE</div>
              </div>

              {g.items.map((t, i, arr) => (
                <div key={t.name} onClick={() => onNavigate && onNavigate('TranscriptDetail')} style={{
                  display: 'grid', gridTemplateColumns: '14px 2fr 80px 80px 80px 110px 60px',
                  gap: 16, padding: '14px 16px',
                  borderBottom: i < arr.length - 1 ? `1px solid ${sl.rule}` : 'none',
                  alignItems: 'center', cursor: 'pointer',
                  background: t.current ? `linear-gradient(90deg, ${sl.cyan}06, transparent 60%)` : 'transparent',
                }}>
                  <SLIcon name="scroll" size={11} color={t.current ? sl.cyan : sl.textFaint}/>
                  <div>
                    <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
                      <div style={{ fontFamily: sl.serif, fontSize: 16, color: sl.text }}>{t.name}</div>
                      {t.current && <span style={{ fontFamily: sl.mono, fontSize: 9.5, padding: '2px 7px', borderRadius: 3, background: sl.cyan + '15', color: sl.cyan, border: `1px solid ${sl.cyan}40`, letterSpacing: '0.12em' }}>VIEWING</span>}
                    </div>
                  </div>
                  <div style={{ fontFamily: sl.mono, fontSize: 11.5, color: sl.textDim, textAlign: 'right' }}>{t.duration}</div>
                  <div style={{ fontFamily: sl.mono, fontSize: 11.5, color: sl.textDim, textAlign: 'right' }}>{t.words.toLocaleString()}</div>
                  <div style={{ fontFamily: sl.mono, fontSize: 11.5, color: sl.textFaint, textAlign: 'right' }}>5</div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                    {t.refined && <span style={{ fontFamily: sl.mono, fontSize: 9.5, padding: '2px 6px', borderRadius: 3, background: sl.green + '12', color: sl.green, letterSpacing: '0.1em', border: `1px solid ${sl.green}30` }}>REFINED</span>}
                    {t.summarized && <span style={{ fontFamily: sl.mono, fontSize: 9.5, padding: '2px 6px', borderRadius: 3, background: sl.cyan + '12', color: sl.cyan, letterSpacing: '0.1em', border: `1px solid ${sl.cyan}40` }}>SUMMARY</span>}
                    {!t.refined && !t.summarized && <span style={{ fontFamily: sl.mono, fontSize: 9.5, color: sl.textFaint, letterSpacing: '0.1em' }}>RAW</span>}
                  </div>
                  <div style={{ fontFamily: sl.mono, fontSize: 11, color: sl.textFaint, textAlign: 'right' }}>{t.date}</div>
                </div>
              ))}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
};

window.StudioTranscriptsList = StudioTranscriptsList;
