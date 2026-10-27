/* Kinaiya trusted audio bridge (web only).
 * Mic capture via MediaRecorder and PCM playback via AudioContext.
 * No network, no inference, no script injection: pure local media only.
 */
(function () {
  'use strict';

  const state = {
    stream: null,
    recorder: null,
    chunks: [],
    mime: '',
    ctx: null,
    sources: [],
    queueTime: 0,
    playing: false,
    // Pending local events polled by Dart: mic level, playback state.
    events: [],
  };

  function supportedMime() {
    const candidates = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg', 'audio/mp4'];
    if (typeof MediaRecorder === 'undefined' || !MediaRecorder.isTypeSupported) {
      return '';
    }
    for (const m of candidates) {
      try {
        if (MediaRecorder.isTypeSupported(m)) return m;
      } catch (e) { /* try next */ }
    }
    return '';
  }

  async function ensureContext(sampleRate) {
    if (!state.ctx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) throw new Error('Web audio unavailable');
      state.ctx = new AC({ sampleRate: sampleRate || 24000 });
    }
    if (state.ctx.state === 'suspended') {
      await state.ctx.resume();
    }
    return state.ctx;
  }

  function pushEvent(e) {
    state.events.push(e);
    if (state.events.length > 64) state.events.shift();
  }

  const api = {
    capabilities() {
      return JSON.stringify({
        mic: !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia),
        playback: !!(window.AudioContext || window.webkitAudioContext),
      });
    },

    async startMic() {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        return 'unavailable';
      }
      api.stopAudio();
      state.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      state.chunks = [];
      state.mime = supportedMime();
      state.recorder = state.mime
        ? new MediaRecorder(state.stream)
        : new MediaRecorder(state.stream);
      state.recorder.ondataavailable = (ev) => {
        if (ev.data && ev.data.size > 0) state.chunks.push(ev.data);
      };
      state.recorder.start(250);
      pushEvent({ type: 'recording', active: true });
      return 'recording';
    },

    async stopMic() {
      const rec = state.recorder;
      if (!rec) return JSON.stringify({ mime: '', base64: '' });
      const done = new Promise((resolve) => {
        rec.onstop = resolve;
      });
      try { rec.stop(); } catch (e) { /* already stopped */ }
      await done;
      const type = rec.mimeType || state.mime || 'audio/webm';
      const blob = new Blob(state.chunks, { type });
      state.chunks = [];
      if (state.stream) {
        for (const t of state.stream.getTracks()) t.stop();
        state.stream = null;
      }
      state.recorder = null;
      pushEvent({ type: 'recording', active: false });
      const buf = await blob.arrayBuffer();
      let bin = '';
      const bytes = new Uint8Array(buf);
      const step = 0x8000;
      for (let i = 0; i < bytes.length; i += step) {
        bin += String.fromCharCode.apply(null, bytes.subarray(i, i + step));
      }
      return JSON.stringify({ mime: type, base64: btoa(bin), bytes: bytes.length });
    },

    cancelMic() {
      try {
        if (state.recorder && state.recorder.state !== 'inactive') state.recorder.stop();
      } catch (e) { /* ignore */ }
      state.chunks = [];
      state.recorder = null;
      if (state.stream) {
        for (const t of state.stream.getTracks()) t.stop();
        state.stream = null;
      }
      pushEvent({ type: 'recording', active: false });
      return 'cancelled';
    },

    async queuePcm(base64, sampleRate) {
      const ctx = await ensureContext(sampleRate || 24000);
      const bin = atob(base64);
      const n = bin.length >> 1;
      if (n === 0) return 'empty';
      const buf = ctx.createBuffer(1, n, sampleRate || 24000);
      const ch = buf.getChannelData(0);
      for (let i = 0; i < n; i++) {
        const lo = bin.charCodeAt(2 * i);
        const hi = bin.charCodeAt(2 * i + 1);
        let v = (hi << 8) | lo;
        if (v >= 32768) v -= 65536;
        ch[i] = v / 32768;
      }
      const src = ctx.createBufferSource();
      src.buffer = buf;
      src.connect(ctx.destination);
      const now = Math.max(ctx.currentTime, state.queueTime);
      src.start(now);
      state.queueTime = now + buf.duration;
      state.sources.push(src);
      src.onended = () => {
        const i = state.sources.indexOf(src);
        if (i >= 0) state.sources.splice(i, 1);
        if (state.sources.length === 0) {
          state.playing = false;
          pushEvent({ type: 'playback', active: false });
        }
      };
      if (!state.playing) {
        state.playing = true;
        pushEvent({ type: 'playback', active: true });
      }
      return 'queued';
    },

    stopAudio() {
      for (const s of state.sources) {
        try { s.stop(); } catch (e) { /* already ended */ }
        try { s.disconnect(); } catch (e) { /* ignore */ }
      }
      state.sources = [];
      state.playing = false;
      state.queueTime = 0;
      if (state.ctx) state.queueTime = state.ctx.currentTime;
      pushEvent({ type: 'playback', active: false });
      return 'stopped';
    },

    playbackActive() {
      return state.sources.length > 0 ? 'yes' : 'no';
    },

    pollEvents() {
      const out = JSON.stringify(state.events);
      state.events = [];
      return out;
    },
  };

  window.kinaiyaAudio = api;
})();
