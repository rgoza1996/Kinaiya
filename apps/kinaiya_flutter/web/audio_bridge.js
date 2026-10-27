/* Kinaiya trusted audio bridge (web only).
 * Mic capture via MediaRecorder and PCM playback via AudioContext.
 * No network, no inference, no script injection: pure local media only.
 *
 * Calling semantics (important for the Dart JS-interop layer):
 * - Sync string returns: capabilities, cancelMic, stopAudio, playbackActive,
 *   pollEvents, playbackInfo, vadSample, vadConfigure, dispose.
 * - Async (Promise) returns: startMic, stopMic, queuePcm, awaitPlayback,
 *   startArmed, stopArmed, beginClip, finishClip.
 * The Dart `_str` helper normalizes both shapes; see
 * `lib/audio/speech_audio_web.dart`. The Node test
 * `web/audio_bridge.test.js` pins these semantics with a fake window.
 */
(function () {
  'use strict';

  var BARGE_SUSTAIN_MS = 220; // sustained voice during playback => barge-in.
  var CLIP_CAP_MS = 60000; // hard 60s cap per voice clip.

  var state = {
    stream: null,
    recorder: null,
    chunks: [],
    mime: '',
    ctx: null,
    analyser: null,
    vadTimer: 0,
    sources: [],
    records: [], // {start, end, duration} per scheduled source.
    queueTime: 0,
    playing: false,
    events: [],
    micGeneration: 0,
    generation: 0, // bumped by stopAudio/dispose; invalidates pending queuePcm.
    waiters: [], // pending awaitPlayback resolvers.
    armed: false,
    clip: { recording: false, startMs: 0 },
    vad: freshVad(),
  };

  function freshVad() {
    return {
      epoch: 0,
      noise: 0.015,
      speaking: false,
      voiceSince: 0,
      lastVoice: 0,
      bargeSince: 0,
      barged: false,
      clipStart: 0,
      capFired: false,
      endSilenceMs: 1500,
      minSpeechMs: 300,
    };
  }

  function nowMs() {
    return Date.now();
  }

  function supportedMime() {
    var candidates = [
      'audio/webm;codecs=opus',
      'audio/webm',
      'audio/ogg',
      'audio/mp4',
    ];
    if (
      typeof MediaRecorder === 'undefined' ||
      !MediaRecorder.isTypeSupported
    ) {
      return '';
    }
    for (var i = 0; i < candidates.length; i++) {
      try {
        if (MediaRecorder.isTypeSupported(candidates[i])) return candidates[i];
      } catch (e) {
        /* try next */
      }
    }
    return '';
  }

  function ensureContext(sampleRate) {
    if (!state.ctx) {
      var AC =
        typeof window !== 'undefined' &&
        (window.AudioContext || window.webkitAudioContext);
      if (!AC) return Promise.reject(new Error('Web audio unavailable'));
      state.ctx = new AC({ sampleRate: sampleRate || 24000 });
    }
    if (state.ctx.state === 'suspended') {
      return state.ctx.resume().then(function () {
        return state.ctx;
      });
    }
    return Promise.resolve(state.ctx);
  }

  function pushEvent(e) {
    state.events.push(e);
    if (state.events.length > 64) state.events.shift();
  }

  function delay(ms) {
    return new Promise(function (res) {
      setTimeout(res, ms);
    });
  }

  function stopTracks() {
    state.micGeneration++;
    pumpStop();
    if (state.stream) {
      try {
        var tracks = state.stream.getTracks();
        for (var i = 0; i < tracks.length; i++) {
          try {
            tracks[i].stop();
          } catch (e) {
            /* ignore */
          }
        }
      } catch (e) {
        /* ignore */
      }
      state.stream = null;
    }
  }

  function finalizeChunks() {
    var type = state.mime || 'audio/webm';
    var blob = null;
    try {
      var B =
        typeof Blob !== 'undefined'
          ? Blob
          : window && window.Blob
            ? window.Blob
            : null;
      if (B) blob = new B(state.chunks, { type: type });
    } catch (e) {
      blob = null;
    }
    var chunks = state.chunks;
    state.chunks = [];
    return { type: type, blob: blob, count: chunks.length };
  }

  function blobToJson(blob, type) {
    if (!blob) {
      return Promise.resolve(JSON.stringify({ mime: type, base64: '' }));
    }
    return blob
      .arrayBuffer()
      .then(function (buf) {
        var bytes = new Uint8Array(buf);
        if (bytes.length === 0) {
          return JSON.stringify({ mime: type, base64: '' });
        }
        var bin = '';
        var step = 0x8000;
        for (var i = 0; i < bytes.length; i += step) {
          bin += String.fromCharCode.apply(
            null,
            bytes.subarray(i, i + step),
          );
        }
        var b64 =
          typeof btoa !== 'undefined'
            ? btoa(bin)
            : window.btoa(bin);
        return JSON.stringify({
          mime: type,
          base64: b64,
          bytes: bytes.length,
        });
      })
      .catch(function () {
        return JSON.stringify({ mime: type, base64: '' });
      });
  }

  function resolveWaiters(value) {
    var pending = state.waiters;
    state.waiters = [];
    for (var i = 0; i < pending.length; i++) {
      try {
        pending[i](value);
      } catch (e) {
        /* ignore */
      }
    }
  }

  function startRecorderOnStream() {
    state.chunks = [];
    state.mime = supportedMime();
    // Pass the selected mime type through; previous code constructed both
    // branches without options, so the selected type was silently dropped.
    var opts = state.mime ? { mimeType: state.mime } : undefined;
    state.recorder = new MediaRecorder(state.stream, opts);
    state.recorder.ondataavailable = function (ev) {
      if (ev && ev.data && ev.data.size > 0) state.chunks.push(ev.data);
    };
    state.recorder.start(250);
  }

  function pumpStart() {
    pumpStop();
    try {
      if (!state.ctx || !state.stream) return;
      if (typeof state.ctx.createAnalyser !== 'function') return;
      if (typeof window === 'undefined') return;
      var src = state.ctx.createMediaStreamSource(state.stream);
      state.analyser = state.ctx.createAnalyser();
      state.analyser.fftSize = 1024;
      src.connect(state.analyser);
      state.vadTimer = setInterval(function () {
        try {
          var arr = new Uint8Array(state.analyser.fftSize);
          state.analyser.getByteTimeDomainData(arr);
          var sum = 0;
          for (var i = 0; i < arr.length; i++) {
            var v = (arr[i] - 128) / 128;
            sum += v * v;
          }
          var rms = Math.sqrt(sum / arr.length);
          var out = api.vadSample(rms, nowMs());
          if (out) {
            try {
              pushEvent(JSON.parse(out));
            } catch (e) {
              /* ignore malformed */
            }
          }
        } catch (e) {
          /* keep pumping */
        }
      }, 100);
      if (state.vadTimer && state.vadTimer.unref) {
        try {
          state.vadTimer.unref();
        } catch (e) {
          /* ignore */
        }
      }
    } catch (e) {
      /* analyser unavailable: manual vadSample still works */
    }
  }

  function pumpStop() {
    if (state.vadTimer) {
      try {
        clearInterval(state.vadTimer);
      } catch (e) {
        /* ignore */
      }
      state.vadTimer = 0;
    }
    if (state.analyser) {
      try {
        state.analyser.disconnect();
      } catch (e) {
        /* ignore */
      }
      state.analyser = null;
    }
  }

  var api = {
    chooseRecording: function () {
      return new Promise(function (resolve) {
        var input=document.createElement('input');
        input.type='file';input.accept='audio/*,.wav,.webm,.ogg,.mp4,.m4a';
        input.addEventListener('cancel',function(){resolve('');},{once:true});
        input.addEventListener('change',async function(){
          var file=input.files && input.files[0];
          if (!file || file.size>10000000) return resolve('');
          var type=file.type || (file.name.endsWith('.wav') ? 'audio/wav' : 'audio/webm');
          var raw=JSON.parse(await blobToJson(file,type));
          raw.filename=file.name.slice(0,128);
          resolve(JSON.stringify(raw));
        },{once:true});
        input.click();
      });
    },
    capabilities: function () {
      var nav =
        typeof navigator !== 'undefined' ? navigator : window && window.navigator;
      var mic = !!(
        nav &&
        nav.mediaDevices &&
        nav.mediaDevices.getUserMedia
      );
      var playback = !!(
        typeof window !== 'undefined' &&
        (window.AudioContext || window.webkitAudioContext)
      );
      return JSON.stringify({ mic: mic, playback: playback });
    },

    startMic: function () {
      var nav =
        typeof navigator !== 'undefined' ? navigator : window && window.navigator;
      if (!nav || !nav.mediaDevices || !nav.mediaDevices.getUserMedia) {
        return Promise.resolve('unavailable');
      }
      api.stopAudio();
      var micOwner = ++state.micGeneration;
      return nav.mediaDevices
        .getUserMedia({ audio: {echoCancellation: true, noiseSuppression: true, autoGainControl: true} })
        .then(function (stream) {
          if (micOwner !== state.micGeneration) {stream.getTracks().forEach(function(t){t.stop();}); return 'unavailable';}
          state.stream = stream;
          startRecorderOnStream();
          pushEvent({ type: 'recording', active: true });
          return 'recording';
        })
        .catch(function () {
          return 'unavailable';
        });
    },

    stopMic: function () {
      var rec = state.recorder;
      // Inactive or missing recorder must resolve promptly (never hang):
      // finalize whatever chunks exist instead of awaiting an onstop that
      // will never fire.
      if (!rec || rec.state === 'inactive') {
        var fin = finalizeChunks();
        state.recorder = null;
        stopTracks();
        pushEvent({ type: 'recording', active: false });
        return blobToJson(fin.blob, fin.type);
      }
      var done = new Promise(function (resolve) {
        rec.onstop = function () {
          resolve(true);
        };
      });
      try {
        rec.stop();
      } catch (e) {
        /* already stopped */
      }
      return Promise.race([done, delay(3000)]).then(function () {
        var fin2 = finalizeChunks();
        state.recorder = null;
        stopTracks();
        pushEvent({ type: 'recording', active: false });
        return blobToJson(fin2.blob, fin2.type);
      });
    },

    cancelMic: function () {
      try {
        if (state.recorder && state.recorder.state !== 'inactive') {
          state.recorder.stop();
        }
      } catch (e) {
        /* ignore */
      }
      state.chunks = [];
      state.recorder = null;
      stopTracks();
      pushEvent({ type: 'recording', active: false });
      return 'cancelled';
    },

    queuePcm: function (base64, sampleRate) {
      // Capture the generation before awaiting resume: stopAudio bumps it,
      // so a stop issued while suspended invalidates this entry and no
      // stale source is ever started.
      var gen = state.generation;
      return ensureContext(sampleRate || 24000).then(function (ctx) {
        if (gen !== state.generation) return 'stopped';
        var bin;
        try {
          bin =
            typeof atob !== 'undefined' ? atob(base64) : window.atob(base64);
        } catch (e) {
          return 'empty';
        }
        var n = bin.length >> 1;
        if (n === 0) return 'empty';
        var rate = sampleRate || 24000;
        var buf = ctx.createBuffer(1, n, rate);
        var ch = buf.getChannelData(0);
        for (var i = 0; i < n; i++) {
          var lo = bin.charCodeAt(2 * i);
          var hi = bin.charCodeAt(2 * i + 1);
          var v = (hi << 8) | lo;
          if (v >= 32768) v -= 65536;
          ch[i] = v / 32768;
        }
        // Re-check after every await: a stop may have landed while the
        // context was resuming or the buffer was decoded.
        if (gen !== state.generation) return 'stopped';
        var src = ctx.createBufferSource();
        src.buffer = buf;
        src.connect(ctx.destination);
        var start = Math.max(ctx.currentTime, state.queueTime);
        try {
          src.start(start);
        } catch (e) {
          return 'stopped';
        }
        var rec = { start: start, end: start + buf.duration, duration: buf.duration };
        state.queueTime = rec.end;
        state.sources.push(src);
        state.records.push(rec);
        if (state.records.length > 128) state.records.shift();
        src.onended = function () {
          if (gen !== state.generation) return;
          var i = state.sources.indexOf(src);
          if (i >= 0) state.sources.splice(i, 1);
          if (state.sources.length === 0) {
            state.playing = false;
            state.queueTime = 0;
            if (state.ctx) {
              try {
                state.queueTime = state.ctx.currentTime;
              } catch (e) {
                /* ignore */
              }
            }
            pushEvent({ type: 'playback', active: false });
            resolveWaiters('done');
          }
        };
        if (!state.playing) {
          state.playing = true;
          pushEvent({ type: 'playback', active: true });
        }
        return 'queued';
      });
    },

    awaitPlayback: function () {
      if (state.sources.length === 0) return Promise.resolve('idle');
      return new Promise(function (resolve) {
        state.waiters.push(resolve);
      });
    },

    playbackInfo: function () {
      return JSON.stringify(
        state.records.map(function (r) {
          return { start: r.start, end: r.end, duration: r.duration };
        }),
      );
    },

    stopAudio: function () {
      // Invalidate anything still waiting on AudioContext.resume first.
      state.generation++;
      for (var i = 0; i < state.sources.length; i++) {
        try {
          state.sources[i].stop();
        } catch (e) {
          /* already ended */
        }
        try {
          state.sources[i].disconnect();
        } catch (e) {
          /* ignore */
        }
      }
      state.sources = [];
      state.records = [];
      state.playing = false;
      state.queueTime = 0;
      if (state.ctx) {
        try {
          state.queueTime = state.ctx.currentTime;
        } catch (e) {
          /* ignore */
        }
      }
      resolveWaiters('stopped');
      pushEvent({ type: 'playback', active: false });
      return 'stopped';
    },

    playbackActive: function () {
      return state.sources.length > 0 ? 'yes' : 'no';
    },

    pollEvents: function () {
      var out = JSON.stringify(state.events);
      state.events = [];
      return out;
    },

    dispose: function () {
      try {
        api.cancelMic();
      } catch (e) {
        /* ignore */
      }
      try {
        api.stopAudio();
      } catch (e) {
        /* ignore */
      }
      pumpStop();
      state.armed = false;
      state.clip = { recording: false, startMs: 0 };
      state.vad = freshVad();
      state.events = [];
      if (state.ctx) {
        try {
          if (typeof state.ctx.close === 'function') state.ctx.close();
        } catch (e) {
          /* ignore */
        }
        state.ctx = null;
      }
      return 'disposed';
    },

    // -- User-armed continuous listening (VAD) ---------------------------
    //
    // The mic is NEVER enabled automatically: startArmed runs only from an
    // explicit user gesture (Live toggle). The stream is kept across clips
    // while armed; stopArmed (user stop / widget unmount) releases everything.

    vadConfigure: function (input) {
      var cfg = input;
      if (typeof cfg === 'string') {
        try {
          cfg = JSON.parse(cfg);
        } catch (e) {
          return 'invalid';
        }
      }
      cfg = cfg || {};
      if (typeof cfg.endSilenceMs === 'number') {
        state.vad.endSilenceMs = Math.min(
          5000,
          Math.max(400, cfg.endSilenceMs | 0),
        );
      }
      if (typeof cfg.minSpeechMs === 'number') {
        state.vad.minSpeechMs = Math.min(
          2000,
          Math.max(100, cfg.minSpeechMs | 0),
        );
      }
      return JSON.stringify({
        endSilenceMs: state.vad.endSilenceMs,
        minSpeechMs: state.vad.minSpeechMs,
        bargeMs: BARGE_SUSTAIN_MS,
        clipCapMs: CLIP_CAP_MS,
      });
    },

    vadSample: function (rms, tMs, epoch) {
      var now = typeof tMs === 'number' ? tMs : nowMs();
      if (typeof epoch === 'number' && epoch !== state.vad.epoch) return '';
      if (!state.armed) return '';
      var r = typeof rms === 'number' ? rms : 0;
      var vad = state.vad;
      // Distinct speech/silence thresholds prevent noisy state flapping.
      if (!vad.speaking && !state.playing && r < vad.noise * 2) {
        vad.noise = Math.max(0.002, Math.min(0.035, 0.98 * vad.noise + 0.02 * r));
      }
      var speech = Math.max(vad.noise * 3.4 + 0.012, 0.026);
      var silence = Math.max(vad.noise * 2.2 + 0.006, 0.012);
      var voice = r > speech || (vad.speaking && r >= silence);
      // Barge timing is independent of ordinary turn-start confirmation.
      if (state.playing && !vad.barged) {
        if (r > speech) {
          if (!vad.bargeSince) vad.bargeSince = now;
          if (now - vad.bargeSince >= BARGE_SUSTAIN_MS) {
            vad.barged = true;
            api.stopAudio(); // Immediate local stop, before any Dart/HTTP await.
            return JSON.stringify({type: 'barge', epoch: vad.epoch});
          }
        } else if (r < silence) vad.bargeSince = 0;
      }
      if (voice) {
        vad.lastVoice = now;
        if (!vad.speaking) {
          if (!vad.voiceSince) vad.voiceSince = now;
          if (now - vad.voiceSince >= vad.minSpeechMs) {
            vad.speaking = true;
            return JSON.stringify({type: 'speech-start', epoch: vad.epoch});
          }
        }
      } else {
        if (!vad.speaking) vad.voiceSince = 0;
        if (vad.speaking && now - vad.lastVoice >= vad.endSilenceMs) {
          vad.speaking = false;
          vad.voiceSince = 0;
          vad.bargeSince = 0;
          vad.barged = false;
          return JSON.stringify({type: 'speech-end', epoch: vad.epoch});
        }
      }
      if (
        !vad.capFired &&
        vad.clipStart &&
        now - vad.clipStart >= CLIP_CAP_MS
      ) {
        vad.capFired = true;
        return JSON.stringify({ type: 'clip-cap', epoch: vad.epoch });
      }
      return '';
    },

    startArmed: function () {
      var nav =
        typeof navigator !== 'undefined' ? navigator : window && window.navigator;
      if (!nav || !nav.mediaDevices || !nav.mediaDevices.getUserMedia) {
        return Promise.resolve('unavailable');
      }
      api.stopAudio();
      var micOwner = ++state.micGeneration;
      var needStream = !state.stream;
      var p = needStream
        ? nav.mediaDevices.getUserMedia({ audio: {echoCancellation: true, noiseSuppression: true, autoGainControl: true} })
        : Promise.resolve(state.stream);
      return p.then(
        async function (stream) {
          if (micOwner !== state.micGeneration) {stream.getTracks().forEach(function(t){t.stop();}); return 'unavailable';}
          try { await ensureContext(24000); } catch(e) {stream.getTracks().forEach(function(t){t.stop();}); return 'unavailable';}
          if (micOwner !== state.micGeneration) {stream.getTracks().forEach(function(t){t.stop();}); return 'unavailable';}
          state.stream = stream;
          state.armed = true;
          state.vad.epoch++;
          state.vad.speaking = false;
          state.vad.voiceSince = 0;
          state.vad.lastVoice = 0;
          state.vad.bargeSince = 0;
          state.vad.barged = false;
          state.vad.clipStart = nowMs();
          state.vad.capFired = false;
          pumpStart();
          pushEvent({ type: 'armed', active: true, epoch: state.vad.epoch });
          return 'armed';
        },
        function () {
          return 'unavailable';
        },
      );
    },

    stopArmed: function () {
      try {
        if (state.recorder && state.recorder.state !== 'inactive') {
          try {
            state.recorder.stop();
          } catch (e) {
            /* ignore */
          }
        }
      } catch (e) {
        /* ignore */
      }
      state.recorder = null;
      state.chunks = [];
      state.clip = { recording: false, startMs: 0 };
      state.armed = false;
      pumpStop();
      stopTracks();
      pushEvent({ type: 'armed', active: false });
      return Promise.resolve('idle');
    },

    beginClip: function () {
      if (!state.armed || !state.stream) {
        return Promise.resolve('not-armed');
      }
      if (state.recorder && state.recorder.state !== 'inactive') {
        return Promise.resolve('busy');
      }
      try {
        startRecorderOnStream();
      } catch (e) {
        return Promise.resolve('unavailable');
      }
      var old = state.vad;
      state.vad = freshVad();
      state.vad.epoch = old.epoch + 1;
      state.vad.noise = old.noise;
      state.vad.endSilenceMs = old.endSilenceMs;
      state.vad.minSpeechMs = old.minSpeechMs;
      state.vad.clipStart = nowMs();
      state.clip = { recording: true, startMs: nowMs() };
      return Promise.resolve('clip-recording');
    },

    captureEpoch: function () { return String(state.vad.epoch); },

    finishClip: function () {
      var rec = state.recorder;
      // Keep the armed stream alive: only the per-clip recorder stops here.
      // Inactive/missing recorder resolves promptly with whatever exists.
      if (!rec || rec.state === 'inactive') {
        var fin = finalizeChunks();
        state.recorder = null;
        state.clip = { recording: false, startMs: 0 };
        return blobToJson(fin.blob, fin.type);
      }
      var done = new Promise(function (resolve) {
        rec.onstop = function () {
          resolve(true);
        };
      });
      try {
        rec.stop();
      } catch (e) {
        /* already stopped */
      }
      return Promise.race([done, delay(3000)]).then(function () {
        var fin2 = finalizeChunks();
        state.recorder = null;
        state.clip = { recording: false, startMs: 0 };
        return blobToJson(fin2.blob, fin2.type);
      });
    },
  };

  if (typeof window !== 'undefined') {
    window.kinaiyaAudio = api;
  } else if (typeof globalThis !== 'undefined') {
    globalThis.kinaiyaAudio = api;
  }
})();
