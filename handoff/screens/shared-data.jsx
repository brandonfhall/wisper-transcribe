// Shared placeholder data used by all three directions.
// Realistic content for a TTRPG transcription tool — a Call of Cthulhu
// campaign (Impossible Landscapes, from the example folder) and a D&D one.

const WISPER_DATA = {
  user: { name: 'Brandon', initials: 'BH' },
  device: { name: 'CUDA · RTX 4070', model: 'large-v3-turbo', hf: true },

  campaigns: [
    { slug: 'impossible-landscapes', name: 'Impossible Landscapes', system: 'Call of Cthulhu', sessions: 7, color: '#7d4d8c' },
    { slug: 'wildwood', name: 'The Wildwood', system: 'D&D 5e', sessions: 14, color: '#4d8c6e' },
    { slug: 'strahd', name: 'Curse of Strahd', system: 'D&D 5e', sessions: 3, color: '#8c4d4d' },
  ],

  speakers: [
    { name: 'Alice',   role: 'Keeper',    initials: 'A', hue: 200, sessions: 24 },
    { name: 'Marcus',  role: 'Investigator', initials: 'M', hue: 30,  sessions: 24 },
    { name: 'Priya',   role: 'Investigator', initials: 'P', hue: 320, sessions: 22 },
    { name: 'Devon',   role: 'Investigator', initials: 'D', hue: 140, sessions: 23 },
    { name: 'Yuki',    role: 'Investigator', initials: 'Y', hue: 60,  sessions: 19 },
    { name: 'Theo',    role: 'Investigator', initials: 'T', hue: 270, sessions: 18 },
  ],

  jobs: [
    { id: 'j_8a1f', title: 'S1 E7 — The Yellow Throne.mp3', stage: 'Diarizing', progress: 0.62, eta: '4 min', campaign: 'impossible-landscapes', startedAt: '2 min ago' },
    { id: 'j_8a20', title: 'S1 E6 — Statues That Weep.mp3', stage: 'Refining', progress: 0.31, eta: '8 min', campaign: 'impossible-landscapes', startedAt: 'just now' },
    { id: 'j_79ce', title: 'Wildwood-014.mp3', stage: 'Queued', progress: 0, eta: '—', campaign: 'wildwood', startedAt: '—' },
  ],

  completed: [
    { name: 'S1 E5 — A Long Bright Hallway', duration: '3h 41m', date: 'Apr 28', campaign: 'impossible-landscapes', words: 28411, refined: true,  summarized: true },
    { name: 'S1 E4 — The Macallistar House', duration: '4h 02m', date: 'Apr 21', campaign: 'impossible-landscapes', words: 31102, refined: true,  summarized: true },
    { name: 'Wildwood-013 — The Mire',       duration: '3h 18m', date: 'Apr 19', campaign: 'wildwood',              words: 24008, refined: true,  summarized: true },
    { name: 'S1 E3 — Kings and Queens',      duration: '3h 55m', date: 'Apr 14', campaign: 'impossible-landscapes', words: 30217, refined: true,  summarized: true, current: true },
    { name: 'S1 E2 — Cumstone, Year One',    duration: '3h 18m', date: 'Apr 07', campaign: 'impossible-landscapes', words: 26418, refined: true,  summarized: true },
    { name: 'Wildwood-012 — Crow Hollow',    duration: '2h 54m', date: 'Apr 05', campaign: 'wildwood',              words: 21887, refined: true,  summarized: false },
    { name: 'S1 E1 — Remove Your Mask',      duration: '4h 11m', date: 'Mar 31', campaign: 'impossible-landscapes', words: 33214, refined: true,  summarized: true },
    { name: 'Strahd S3 — The Mists',         duration: '3h 28m', date: 'Mar 24', campaign: 'strahd',                words: 25901, refined: false, summarized: false },
    { name: 'Wildwood-011 — Old Bones',      duration: '3h 02m', date: 'Mar 22', campaign: 'wildwood',              words: 22117, refined: true,  summarized: true },
  ],

  // Marquee transcript: S1 E3 — Kings and Queens
  transcript: {
    title: 'Kings and Queens',
    episode: 'S1 E3',
    campaign: 'Impossible Landscapes',
    system: 'Call of Cthulhu',
    date: 'April 14, 2026',
    duration: '3h 55m',
    speakers: 5,
    words: 30217,
    sanity: 'Tense',

    recap: [
      'After surviving the Macallistar house, the investigators woke in a hotel room they did not check into. Marcus found a brass key in his coat pocket; none of them remembered the King in Yellow paperback on the nightstand, but everyone agreed it had not been there the night before.',
      'They tracked Abigail Wright to a lecture hall in Princeton. She did not recognize them, but called Devon by a name his mother used. The conversation derailed when a man in a yellow coat passed the window — only Yuki saw him.',
      'The session ended with the party splitting: Marcus and Priya broke into Abigail\'s office, Devon and Yuki followed the man in yellow to a parking garage. Theo stayed in the car. Theo is no longer in the car.',
    ],

    loot: [
      { name: 'Brass key, unmarked',     where: 'Marcus, hotel coat pocket', mundane: false },
      { name: 'Annotated paperback',     where: 'Hotel nightstand',          mundane: false, note: 'The King in Yellow, 1895 ed. Margin notes in three hands.' },
      { name: 'Princeton faculty roster', where: 'Abigail\'s office drawer',  mundane: true },
      { name: 'Yellow lapel pin',         where: 'Parking garage floor',      mundane: false, note: 'Spiral motif. Cold to the touch even in sun.' },
    ],

    npcs: [
      { name: 'Abigail Wright',       role: 'Professor, Princeton',   status: 'Alive',     note: 'Called Devon "Daniel". Does not remember meeting party.' },
      { name: 'The Man in Yellow',    role: 'Unknown',                status: 'Unknown',   note: 'Only Yuki could see him through the window.' },
      { name: 'Theo Vance',           role: 'Investigator',           status: 'Missing',   note: 'Last seen in the rental car at 11:42 PM.' },
      { name: 'Hotel night clerk',    role: 'NPC',                    status: 'Alive',     note: 'Claimed party checked in three days ago. Party arrived today.' },
    ],

    followups: [
      'Who put the paperback in the hotel room?',
      'Find out who "Daniel" is to Devon\'s family.',
      'Return to the Macallistar house in daylight — does the basement door exist?',
      'Theo\'s phone last pinged a tower 80 miles north. Why?',
    ],

    quotes: [
      { speaker: 'Yuki',   text: '"He waved at me. He waved at me and no one else saw it."' },
      { speaker: 'Marcus', text: '"I don\'t want the key. I want the key to never have been in my pocket."' },
      { speaker: 'Alice',  text: '"Roll a Sanity check. No, both of you."' },
    ],

    excerpt: [
      { t: '02:14:08', speaker: 'Yuki',   text: 'There\'s a man in a yellow coat at the window. He\'s — he\'s looking right at me.' },
      { t: '02:14:11', speaker: 'Alice',  text: 'Everyone else, give me a Spot Hidden. … Anyone? Nobody else sees him.' },
      { t: '02:14:18', speaker: 'Devon',  text: 'I\'m looking right where she\'s pointing. There\'s nothing there.' },
      { t: '02:14:22', speaker: 'Yuki',   text: 'He waved at me. He waved at me and no one else saw it.' },
      { t: '02:14:27', speaker: 'Marcus', text: 'Okay. Okay. We are leaving. Now.' },
    ],
  },

  // Live recording mock
  live: {
    elapsed: '01:24:11',
    channel: '#table-1 · The Crooked Coffer',
    speaking: 'Priya',
    levels: [
      { name: 'Alice',  level: 0.12, isSpeaking: false, hue: 200 },
      { name: 'Marcus', level: 0.04, isSpeaking: false, hue: 30 },
      { name: 'Priya',  level: 0.78, isSpeaking: true,  hue: 320 },
      { name: 'Devon',  level: 0.02, isSpeaking: false, hue: 140 },
      { name: 'Yuki',   level: 0.18, isSpeaking: false, hue: 60 },
    ],
  },

  // Recordings list — raw Discord captures in various states
  recordings: [
    { id: 'r_a8f1c2', name: 'The Yellow Throne, take 1', campaign: 'impossible-landscapes', status: 'recording',     duration: '01:24:11', segments: 84,  channel: '#table-1', startedAt: 'Apr 28 · 20:18' },
    { id: 'r_92e4d8', name: 'Wildwood Session 14',       campaign: 'wildwood',              status: 'transcribing', duration: '03:18:47', segments: 199, channel: '#wildwood', startedAt: 'Apr 26 · 19:05', jobId: 'j_79ce' },
    { id: 'r_71b3a1', name: 'Statues That Weep',          campaign: 'impossible-landscapes', status: 'done',         duration: '04:02:18', segments: 243, channel: '#table-1', startedAt: 'Apr 21 · 20:24', transcript: 'S1E6-statues-that-weep' },
    { id: 'r_61c2b9', name: 'A Long Bright Hallway',      campaign: 'impossible-landscapes', status: 'done',         duration: '03:41:02', segments: 222, channel: '#table-1', startedAt: 'Apr 14 · 20:11', transcript: 'S1E5-a-long-bright-hallway' },
    { id: 'r_5f8a14', name: 'Wildwood Session 13',        campaign: 'wildwood',              status: 'done',         duration: '03:18:24', segments: 199, channel: '#wildwood', startedAt: 'Apr 12 · 19:08', transcript: 'WW-013-the-mire' },
    { id: 'r_44b1c0', name: 'Kings and Queens',           campaign: 'impossible-landscapes', status: 'done',         duration: '03:55:00', segments: 235, channel: '#table-1', startedAt: 'Apr 14 · 20:00', transcript: 'S1E3-kings-and-queens' },
    { id: 'r_3a2e88', name: 'Cumstone, Year One',         campaign: 'impossible-landscapes', status: 'failed',       duration: '00:08:42', segments: 9,   channel: '#table-1', startedAt: 'Apr 07 · 20:02', error: 'Bot disconnected · code 4014' },
  ],

  discordPresets: [
    { name: 'My Server — table-1',    guild: '987654321098765432', channel: '123456789012345678' },
    { name: 'My Server — wildwood',   guild: '987654321098765432', channel: '123456789012345679' },
    { name: 'Strahd group — voice',   guild: '887654321098765432', channel: '423456789012345678' },
  ],
};

window.WISPER_DATA = WISPER_DATA;
