// Prototype shell — owns nav state and renders the active Studio screen.
// Sidebar links route via onNavigate; deep links via location.hash work too.

const { useState, useEffect, useCallback } = React;

const SCREENS = {
  'Dashboard':       () => <window.StudioDashboard        onNavigate={window.__navProto}/>,
  'Transcribe':      () => <window.StudioTranscribe       onNavigate={window.__navProto}/>,
  'Record':          () => <window.StudioRecord           onNavigate={window.__navProto}/>,
  'Recordings':      () => <window.StudioRecordings       onNavigate={window.__navProto}/>,
  'Transcripts':     () => <window.StudioTranscriptsList  onNavigate={window.__navProto}/>,
  'TranscriptDetail':() => <window.StudioTranscript       onNavigate={window.__navProto}/>,
  'Speakers':        () => <window.StudioSpeakers         onNavigate={window.__navProto}/>,
  'Campaigns':       () => <window.StudioCampaigns        onNavigate={window.__navProto}/>,
  'Config':          () => <window.StudioConfig           onNavigate={window.__navProto}/>,
};

const Prototype = () => {
  const [screen, setScreen] = useState(() => {
    const h = location.hash.replace('#', '');
    return SCREENS[h] ? h : 'Dashboard';
  });

  const navigate = useCallback((next) => {
    // Some legacy nav targets aren't first-class screens — map them.
    const aliases = { 'Transcripts': 'Transcripts' };
    const target = SCREENS[next] ? next : (aliases[next] || 'Dashboard');
    setScreen(target);
    location.hash = target;
    window.scrollTo(0, 0);
  }, []);

  // Expose for inline child links (avoids prop-drilling through deeply nested
  // inline-styled elements — kept as a single function reference).
  window.__navProto = navigate;

  useEffect(() => {
    const onHash = () => {
      const h = location.hash.replace('#', '');
      if (SCREENS[h]) setScreen(h);
    };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  const Screen = SCREENS[screen];
  return (
    <div className="proto-shell">
      <Screen/>
    </div>
  );
};

ReactDOM.createRoot(document.getElementById('root')).render(<Prototype/>);
