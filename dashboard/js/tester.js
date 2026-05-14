// dashboard/js/tester.js — Panel 1: Local Agent Tester
// Flow:
//   IDLE       → "Start Call" button visible, input bar HIDDEN
//   CONNECTING → spinner shown immediately on click, greeting fetched in background
//   GREETING   → CMC Assistant's lang-selection greeting plays, 3 lang buttons shown AFTER audio ends
//   PHASE 1    → user clicks 🇱🇰/🇮🇳/🇬🇧 button → language locked
//   PHASE 2    → normal support chat, input bar SHOWN

const Tester = (() => {
  let history       = [];
  let turnCount     = 0;
  let lockedLang    = null;   // null = not yet chosen
  let callActive    = false;  // false = idle
  let mediaRecorder = null;
  let audioChunks   = [];
  let isRecording   = false;
  // Pre-unlocked Audio element (created synchronously on click to beat autoplay block)
  let _greetingAudio = null;
  // Flag so we only render lang buttons once
  let _langButtonsRendered = false;
  // Inactivity timers (two-strike system)
  let _inactivityTimer     = null;
  let _secondChanceTimer   = null;
  const INACTIVITY_MS      = 30000; // 30 s — first silence before strike 1
  const SECOND_CHANCE_MS   = 20000; // 20 s — window after strike 1 before goodbye
  // Track the last agent reply so we can replay it on a "yes" response
  let _lastAgentAudioB64   = null;
  let _lastAgentText       = null;
  // Track whether we are in a "waiting for yes" state (strike 1 fired)
  let _waitingForYes       = false;

  // "Are you still there?" prompts — displayed as fallback text if TTS fails
  const STILL_THERE = {
    si: 'ඔබ තවම රැදී සිටිනවද?',
    ta: 'நீங்கள் கோட்டில் இருக்கிறீர்களா?',
    en: 'Are you still there?',
  };

  // Yes-keywords for client-side detection (mirrors language.py)
  const YES_KEYWORDS = {
    si: ['ඔව්', 'ඔව් ඔව්', 'හරි', 'ඔව් හරි', 'ඔවු', 'yes', 'ok', 'okay'],
    ta: ['ஆமாம்', 'சரி', 'ஆம்', 'ஆமா', 'yes', 'ok', 'okay'],
    en: ['yes', 'yeah', 'yep', 'yup', 'sure', 'ok', 'okay',
         'correct', 'still here', "i'm here", 'im here', 'still there'],
  };

  function detectYes(text, lang) {
    const lower = text.toLowerCase().trim();
    const kws = [...(YES_KEYWORDS[lang] || []), ...YES_KEYWORDS.en];
    return kws.some(kw => lower === kw || lower.includes(kw));
  }

  const LANG_FLAGS = { si: '🇱🇰 සිංහල', en: '🇬🇧 English', ta: '🇮🇳 தமிழ்' };
  const LANG_BADGE = { si: 'badge-si', en: 'badge-en', ta: 'badge-ta' };
  const LANG_NAMES = { si: 'Sinhala', en: 'English', ta: 'Tamil' };

  // ── DOM helpers ─────────────────────────────────────────────────
  const $ = id => document.getElementById(id);
  const chatMessages = () => $('chatMessages');

  // ── Inactivity timer (two-strike) ──────────────────────────────────────
  // Strike 1: fires after 30 s of silence → AI speaks "are you still there?"
  // Strike 2: fires 20 s after strike 1 if still no response → AI speaks goodbye + call ends
  function startInactivityTimer() {
    clearInactivityTimer();
    if (!callActive || !lockedLang) return;
    _inactivityTimer = setTimeout(() => {
      if (!callActive || !lockedLang) return;
      _fireStrikeOne();
    }, INACTIVITY_MS);
  }

  function clearInactivityTimer() {
    if (_inactivityTimer)   { clearTimeout(_inactivityTimer);   _inactivityTimer   = null; }
    if (_secondChanceTimer) { clearTimeout(_secondChanceTimer); _secondChanceTimer = null; }
    _waitingForYes = false;
  }

  // ── Strike 1: call TTS endpoint and play the "still there?" audio ────────────
  async function _fireStrikeOne() {
    if (!callActive || !lockedLang) return;
    const lang = lockedLang;

    // Fetch TTS audio from backend
    let text = STILL_THERE[lang] || STILL_THERE.en;
    let audioB64 = null;
    try {
      const res = await fetch('/dashboard/api/test-still-there', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lang }),
      });
      const data = await res.json();
      text     = data.text    || text;
      audioB64 = data.audio_b64 || null;
    } catch (e) {
      console.warn('still-there TTS fetch failed, using fallback text:', e);
    }

    // Show bubble and play audio
    addBubble('agent', text, lang, audioB64, true);
    chatMessages().scrollTop = chatMessages().scrollHeight;
    _waitingForYes = true;

    // Arm strike 2 (20 s second-chance window)
    _secondChanceTimer = setTimeout(() => {
      if (!callActive || !lockedLang || !_waitingForYes) return;
      _fireStrikeTwo();
    }, SECOND_CHANCE_MS);
  }

  // ── Strike 2: play goodbye + end call ─────────────────────────────────
  async function _fireStrikeTwo() {
    if (!callActive) return;
    const lang = lockedLang || 'en';

    // Fetch goodbye TTS from backend
    const GOODBYE_FALLBACK = {
      si: 'ඔබ ප්‍රතිචාර නොදීම නිසා, අපි ඇමතුම අවසන් කරනවා. ස්තූතියි.',
      ta: 'பதில் இல்லாததால் அழைப்பை முடிக்கிறோம். நன்றி.',
      en: "We haven't heard from you, so we'll end the call now. Thank you.",
    };
    let text = GOODBYE_FALLBACK[lang] || GOODBYE_FALLBACK.en;
    let audioB64 = null;
    try {
      const res = await fetch('/dashboard/api/test-end-call', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lang }),
      });
      const data = await res.json();
      text     = data.text      || text;
      audioB64 = data.audio_b64 || null;
    } catch (e) {
      console.warn('test-end-call TTS fetch failed, using fallback text:', e);
    }

    // Show goodbye bubble, play audio, then reset after 3 s
    addBubble('agent', text, lang, audioB64, true);
    addBanner('📞 Call ended due to inactivity.', 'info');
    chatMessages().scrollTop = chatMessages().scrollHeight;

    setTimeout(() => resetToLanding(), 3000);
  }

  // ── Bubble renderer ─────────────────────────────────────────────
  function addBubble(role, text, lang, audioB64, isSystemPrompt = false) {
    const wrap = document.createElement('div');
    wrap.className = `chat-bubble ${role}`;

    const meta = document.createElement('div');
    meta.className = 'bubble-meta';
    const badge = lang
      ? `<span class="badge ${LANG_BADGE[lang] || 'badge-ok'}">${LANG_FLAGS[lang] || lang}</span>`
      : '';
    meta.innerHTML = role === 'user'
      ? `<span>You</span>${badge}`
      : `${badge}<span>CMC Assistant</span>`;

    const body = document.createElement('div');
    body.className = 'bubble-body';
    body.textContent = text;

    wrap.appendChild(meta);
    wrap.appendChild(body);

    if (audioB64 && role === 'agent') {
      const audioDiv = document.createElement('div');
      audioDiv.className = 'bubble-audio';
      const aud = document.createElement('audio');
      aud.controls = true;
      aud.src = `data:audio/mp3;base64,${audioB64}`;
      audioDiv.appendChild(aud);
      wrap.appendChild(audioDiv);
      aud.play().catch(() => {});
    }

    chatMessages().appendChild(wrap);
    chatMessages().scrollTop = chatMessages().scrollHeight;

    // Track last agent reply for "yes" replay
    if (role === 'agent' && !isSystemPrompt) {
      _lastAgentText    = text;
      _lastAgentAudioB64 = audioB64 || null;
    }

    return wrap;
  }

  function addBanner(text, type = 'info') {
    const div = document.createElement('div');
    div.className = type === 'escalate' ? 'escalate-banner' : 'lang-confirm-banner';
    div.textContent = text;
    chatMessages().appendChild(div);
    chatMessages().scrollTop = chatMessages().scrollHeight;
  }

  // ── Connecting spinner (Step 3) ──────────────────────────────────
  function showConnectingState() {
    const landing = $('callLanding');
    if (landing) landing.remove();

    const div = document.createElement('div');
    div.className = 'connecting-state';
    div.id = 'connectingState';
    div.innerHTML = `
      <div class="connecting-ring"></div>
      <div class="connecting-label">Connecting…</div>`;
    chatMessages().appendChild(div);
    chatMessages().scrollTop = chatMessages().scrollHeight;
  }

  function removeConnectingState() {
    const el = $('connectingState');
    if (el) el.remove();
  }

  // ── Pipeline indicator ──────────────────────────────────────────
  function setPipeline(stt, llm, tts) {
    [['step-stt', stt], ['step-llm', llm], ['step-tts', tts]].forEach(([id, state]) => {
      document.querySelector(`#${id} .step-dot`).className = `step-dot ${state}`;
      document.querySelector(`#${id} .step-status`).textContent =
        state === 'active' ? '…' : state === 'done' ? '✓' : state === 'error' ? '✗' : '—';
    });
  }

  function setStatus(text, type) {
    const el = $('sessionStatus');
    el.textContent = text;
    el.className = `badge badge-${type}`;
  }

  function updateCallState(state) {
    const label = $('callStateLabel');
    const map = {
      idle:           ['Idle',          'badge-warn'],
      connecting:     ['Connecting…',   'badge-warn'],
      greeting:       ['Greeting…',     'badge-warn'],
      'lang-select':  ['Picking lang…', 'badge-warn'],
      active:         ['In Call',       'badge-ok'],
    };
    const [text, cls] = map[state] || ['—', 'badge-warn'];
    label.textContent = text;
    label.className = `badge ${cls}`;
  }

  function updateLangStatus() {
    const el = $('langLockStatus');
    if (!el) return;
    if (lockedLang) {
      el.innerHTML = `<span class="badge ${LANG_BADGE[lockedLang]}">🔒 ${LANG_NAMES[lockedLang]}</span>`;
      $('chatInput').placeholder = `Type in ${LANG_NAMES[lockedLang]}…`;
    } else if (callActive) {
      el.innerHTML = `<span class="badge badge-warn">⏳ Picking language</span>`;
      $('chatInput').placeholder = `Speak or type your language…`;
    } else {
      el.innerHTML = `<span class="badge badge-warn">⏳ Not set</span>`;
    }
  }

  function updateInfo(lang, escalate) {
    $('turnCount').textContent = turnCount;
    $('lastLang').innerHTML = lang
      ? `<span class="badge ${LANG_BADGE[lang]}">${LANG_FLAGS[lang]}</span>` : '—';
    const esc = $('escalateStatus');
    esc.textContent = escalate ? 'Yes ⚠️' : 'No';
    esc.style.color = escalate ? 'var(--danger)' : '';
  }

  // ── Start Call — Step 3: connecting animation immediately ────────
  async function startCall() {
    callActive = true;
    lockedLang = null;
    history    = [];
    turnCount  = 0;
    _langButtonsRendered = false;

    // ── KEY: unlock Audio element SYNCHRONOUSLY inside the user-gesture tick
    _greetingAudio = new Audio();
    _greetingAudio.volume = 1.0;
    _greetingAudio.src = 'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=';
    _greetingAudio.play().catch(() => {});

    // Keep input bar visible so user can speak immediately
    $('chatInputBar').classList.remove('hidden');

    // Step 3: show connecting spinner RIGHT NOW (synchronous)
    showConnectingState();
    updateCallState('connecting');
    setStatus('Connecting…', 'warn');
    updateLangStatus();

    // Fetch greeting in background
    await showGreeting();
  }

  async function showGreeting() {
    try {
      const res  = await fetch('/dashboard/api/test-greeting');
      const data = await res.json();

      // Remove spinner
      removeConnectingState();

      // Show the text bubble (no inline audio — we use the pre-unlocked element)
      addBubble('agent', data.greeting, null, null);
      updateCallState('greeting');
      setStatus('Playing greeting…', 'warn');

      // Play greeting audio; show lang buttons only AFTER audio ends (Step 3)
      if (data.audio_b64 && _greetingAudio) {
        _greetingAudio.src = `data:audio/mp3;base64,${data.audio_b64}`;
        _greetingAudio.load();

        // Show lang buttons once audio finishes (or on error — fallback)
        _greetingAudio.onended = () => {
          updateCallState('lang-select');
          setStatus('Speak your language…', 'warn');
        };

        _greetingAudio.play().catch(err => {
          console.warn('Greeting autoplay blocked:', err);
          _addManualPlayBtn(_greetingAudio);
          updateCallState('lang-select');
          setStatus('Speak your language…', 'warn');
        });
      } else {
        updateCallState('lang-select');
        setStatus('Speak your language…', 'warn');
      }
    } catch (err) {
      console.error('Greeting fetch failed:', err);
      removeConnectingState();
      addBubble('agent',
        'ආයුබෝවන්! வணக்கம்! Hello! — Please speak your language.',
        null, null);
      updateCallState('lang-select');
      setStatus('Speak your language…', 'warn');
    }
  }

  // Append a small play button to the most recent agent bubble
  function _addManualPlayBtn(audioEl) {
    const bubbles = chatMessages().querySelectorAll('.chat-bubble.agent');
    const last    = bubbles[bubbles.length - 1];
    if (!last) return;
    const btn = document.createElement('button');
    btn.className = 'play-greeting-btn';
    btn.innerHTML = '🔊 Tap to hear greeting';
    btn.onclick = () => { audioEl.play().catch(() => {}); btn.remove(); };
    last.appendChild(btn);
  }

  function stopGreetingAudio() {
    if (_greetingAudio && !_greetingAudio.paused) {
      _greetingAudio.pause();
      _greetingAudio.currentTime = 0;
    }
  }

  // ── Phase 1: language selection via button click (Step 2) ────────
  // `langCode` is 'si' | 'ta' | 'en' — passed directly, no text parsing needed
  async function sendLangChoice(langCode) {
    stopGreetingAudio();
    setStatus('Confirming language…', 'warn');
    setPipeline('', 'active', '');

    try {
      const res = await fetch('/dashboard/api/test-text', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        // Step 2: pass lang_code directly so server skips detect_language_choice()
        body: JSON.stringify({ message: langCode, history, lang_phase: 1, lang_code: langCode }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Request failed');

      setPipeline('done', 'done', 'done');
      addBubble('agent', data.reply, data.lang, data.audio_b64);

      if (data.chosen_lang) {
        lockedLang = data.chosen_lang;
        addBanner(`✅ Language locked to ${LANG_NAMES[lockedLang]}. Now ask your question.`);
        updateCallState('active');
        updateLangStatus();
        setStatus('In Call', 'ok');
        App.toast(`Language set to ${LANG_NAMES[lockedLang]}`, 'ok');

        // Input bar is already visible, just update focus
        $('chatInput').focus();
      } else {
        // Shouldn't happen when sending a direct code, but handle gracefully
        addBanner('❓ Something went wrong — please try again');
        setStatus('Speak your language…', 'warn');
      }
    } catch (e) {
      setPipeline('', '', 'error');
      setStatus('Error', 'danger');
      App.toast(e.message, 'error');
      // Re-enable buttons on error
      document.querySelectorAll('.lang-choice-btn').forEach(b => { b.disabled = false; });
    }
  }

  // ── Phase 2: support chat ──────────────────────────────────────────
  async function sendSupportMessage(text) {
    stopGreetingAudio();
    clearInactivityTimer(); // user is speaking — cancel silence check
    _waitingForYes = false;

    // ── Yes-detection: replay last question instead of calling LLM (only if waiting) ──
    if (_waitingForYes && detectYes(text, lockedLang) && _lastAgentText) {
      addBubble('user', text, lockedLang);
      // Short status flash
      setStatus('Replaying…', 'warn');
      // Re-show last agent bubble with its audio (replay)
      addBubble('agent', _lastAgentText, lockedLang, _lastAgentAudioB64);
      chatMessages().scrollTop = chatMessages().scrollHeight;
      setStatus('In Call', 'ok');
      startInactivityTimer();
      $('chatInput').value = '';
      return;
    }

    addBubble('user', text, lockedLang);
    setStatus('Thinking…', 'warn');
    setPipeline('', 'active', '');
    $('sendBtn').disabled = true;

    try {
      const res = await fetch('/dashboard/api/test-text', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: text,
          history,
          lang_phase: 2,
          locked_lang: lockedLang,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Request failed');

      history = data.history || [];
      turnCount++;
      setPipeline('done', 'done', 'done');
      addBubble('agent', data.reply, data.lang, data.audio_b64);
      if (data.escalate) addBanner('⚠️ Agent would escalate this call to a human.', 'escalate');
      updateInfo(data.lang, data.escalate);
      setStatus('In Call', 'ok');
      startInactivityTimer(); // restart timer after AI speaks
    } catch (e) {
      setPipeline('', '', 'error');
      setStatus('Error', 'danger');
      App.toast(e.message, 'error');
    } finally {
      $('sendBtn').disabled = false;
      $('chatInput').value = '';
    }
  }

  // ── Main text dispatcher (Phase 2 only — Phase 1 uses buttons) ──
  async function sendText() {
    if (!callActive) return;
    const text = $('chatInput').value.trim();
    if (!text) return;

    stopGreetingAudio();

    if (!lockedLang) {
      $('chatInput').value = '';
      await sendLangChoice(text);
      return;
    }

    await sendSupportMessage(text);
  }

  // ── Voice recording ─────────────────────────────────────────────
  async function startRecording() {
    if (!callActive || isRecording) return;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      audioChunks  = [];
      mediaRecorder = new MediaRecorder(stream);
      mediaRecorder.ondataavailable = e => { if (e.data.size > 0) audioChunks.push(e.data); };
      mediaRecorder.start(100);
      isRecording = true;
      $('micBtn').classList.add('recording');
      $('recordingBar').classList.remove('hidden');
    } catch {
      App.toast('Microphone access denied', 'error');
    }
  }

  async function stopRecording() {
    if (!isRecording || !mediaRecorder) return;
    isRecording = false;
    $('micBtn').classList.remove('recording');
    $('recordingBar').classList.add('hidden');

    stopGreetingAudio();

    await new Promise(resolve => { mediaRecorder.onstop = resolve; mediaRecorder.stop(); });
    mediaRecorder.stream.getTracks().forEach(t => t.stop());

    const blob = new Blob(audioChunks, { type: 'audio/webm' });
    if (blob.size < 1000) { App.toast('Recording too short', 'warn'); return; }

    setStatus('Processing…', 'warn');
    setPipeline('active', '', '');

    const form = new FormData();
    form.append('audio', blob, 'recording.webm');
    form.append('history', JSON.stringify(history));
    form.append('lang_phase', lockedLang ? 2 : 1);
    form.append('locked_lang', lockedLang || '');

    // If we're waiting for a "yes" after a timeout, don't hit the LLM yet.
    // Just get the transcript first to avoid desyncing the backend history.
    const wasWaiting = _waitingForYes;
    if (wasWaiting) {
      form.append('stt_only', 'true');
    }

    try {
      const res  = await fetch('/dashboard/api/test-voice', { method: 'POST', body: form });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Voice failed');
      if (data.error) { App.toast(data.error, 'warn'); setStatus('In Call', 'ok'); return; }

      const transcript = data.transcript || '';

      if (!lockedLang) {
        // Still in language-selection phase — treat transcript as Phase 1
        $('chatInput').value = transcript;
        await sendLangChoice(transcript);
        return;
      }

      addBubble('user', transcript, lockedLang);
      setPipeline('done', 'done', 'done');

      // ── Yes-detection on voice: replay last question if applicable ────────
      clearInactivityTimer();
      _waitingForYes = false;

      if (wasWaiting) {
        if (detectYes(transcript, lockedLang) && _lastAgentText) {
          setStatus('Replaying…', 'warn');
          addBubble('agent', _lastAgentText, lockedLang, _lastAgentAudioB64);
          chatMessages().scrollTop = chatMessages().scrollHeight;
          setStatus('In Call', 'ok');
          startInactivityTimer();
          return;
        } else {
          // It was NOT a "yes", they actually said something else.
          // Since we passed stt_only, we need to send it to the LLM now using text fallback.
          return sendSupportMessage(transcript);
        }
      }

      history = data.history || [];
      turnCount++;
      addBubble('agent', data.reply, data.lang, data.audio_b64);
      if (data.escalate) addBanner('⚠️ Agent would escalate this call to a human.', 'escalate');
      updateInfo(data.lang, data.escalate);
      setStatus('In Call', 'ok');
      startInactivityTimer(); // restart timer after AI speaks
    } catch (e) {
      setPipeline('', '', 'error');
      setStatus('Error', 'danger');
      App.toast(e.message, 'error');
    }
  }

  // ── New Call (reset to landing) ────────────────────────────────────
  function resetToLanding() {
    clearInactivityTimer();
    history    = [];
    turnCount  = 0;
    lockedLang = null;
    callActive = false;
    _langButtonsRendered = false;
    _lastAgentAudioB64   = null;
    _lastAgentText       = null;
    _waitingForYes       = false;

    chatMessages().innerHTML = `
      <div class="call-landing" id="callLanding">
        <div class="call-bg-dots">
          <span></span><span></span><span></span>
          <span></span><span></span><span></span>
        </div>
        <div class="call-card">
          <div class="call-avatar-ring">
            <div class="call-avatar">🇱🇰</div>
          </div>
          <div class="call-card-title">Colombo Municipal Council</div>
          <div class="call-card-sub">CMC Assistant — AI Trilingual Agent</div>
          <div class="call-card-langs">
            <span class="badge badge-si">සිංහල</span>
            <span class="badge badge-ta">தமிழ்</span>
            <span class="badge badge-en">English</span>
          </div>
          <button class="call-phone-btn" id="startCallBtn">
            <div class="call-phone-ripple"></div>
            <div class="call-phone-ripple delay1"></div>
            <div class="call-phone-circle">
              <svg viewBox="0 0 24 24" fill="white" width="32" height="32">
                <path d="M6.6 10.8c1.4 2.8 3.8 5.1 6.6 6.6l2.2-2.2c.3-.3.7-.4 1-.2 1.1.4 2.3.6 3.6.6.6 0 1 .4 1 1V20c0 .6-.4 1-1 1-9.4 0-17-7.6-17-17 0-.6.4-1 1-1h3.5c.6 0 1 .4 1 1 0 1.3.2 2.5.6 3.6.1.3 0 .7-.2 1L6.6 10.8z"/>
              </svg>
            </div>
          </button>
          <div class="call-phone-label">Start Call</div>
        </div>
      </div>`;

    // Step 4: keep input bar hidden on reset
    $('chatInputBar').classList.add('hidden');
    $('chatInput').value = '';
    $('chatInput').placeholder = 'Type a message...';
    $('startCallBtn').addEventListener('click', startCall);

    setPipeline('', '', '');
    updateInfo('', false);
    updateCallState('idle');
    updateLangStatus();
    setStatus('Ready', 'ok');
    App.toast('Session reset', 'ok');
  }

  // ── Init ────────────────────────────────────────────────────────
  function init() {
    $('sendBtn').addEventListener('click', sendText);
    $('chatInput').addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendText(); }
    });
    $('micBtn').addEventListener('mousedown', startRecording);
    $('micBtn').addEventListener('touchstart', e => { e.preventDefault(); startRecording(); });
    $('micBtn').addEventListener('mouseup', stopRecording);
    $('micBtn').addEventListener('touchend', stopRecording);
    $('clearSessionBtn').addEventListener('click', resetToLanding);

    // Wire up the initial Start Call button
    const startBtn = $('startCallBtn');
    if (startBtn) startBtn.addEventListener('click', startCall);

    // Step 4: ensure input bar is hidden at startup
    $('chatInputBar').classList.add('hidden');

    // Set initial sidebar state
    updateCallState('idle');
    updateLangStatus();
  }

  return { init };
})();
