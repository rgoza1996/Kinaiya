/* Kinaiya trusted audio bridge (web only).
 * Mic capture via MediaRecorder and PCM playback via AudioContext.
 * No network, no inference, no script injection: pure local media only.
 *
 * Calling semantics (important for the Dart JS-interop layer):
 * - Sync string returns: capabilities, cancelMic, stopAudio, playbackActive,
 *   pollEvents, playbackInfo, mouthFrame, vadSample, vadConfigure, dispose.
 * - Async (Promise) returns: startMic, stopMic, queuePcm, queuePcmWithVisemes, awaitPlayback,
 *   startArmed, stopArmed, beginClip, finishClip.
 * The Dart `_str` helper normalizes both shapes; see
 * `lib/audio/speech_audio_web.dart`. The Node test
 * `web/audio_bridge.test.js` pins these semantics with a fake window.
 */
(function () {
  'use strict';

  var BARGE_SUSTAIN_MS = 220; // sustained voice during playback => barge-in.
  var CLIP_CAP_MS = 60000; // hard 60s cap per voice clip.
  var MOUTH_WINDOW_SECONDS = 0.020;
  var MOUTH_MAX_WINDOWS = 1000;
  var MOUTH_MAX_CHUNKS = 128;
  var VISEME_OPENNESS = {
    sil: 0, PP: 0, FF: 0.35, TH: 0.5, DD: 0.5, kk: 0.5,
    CH: 0.5, SS: 0.4, nn: 0.3, RR: 0.5, aa: 1,
    E: 0.7, ih: 0.5, oh: 0.8, ou: 0.6,
  };

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
    // Compact RMS windows only: never retain a second copy of the PCM.
    mouthRecords: [],
    mouth: { openness: 0, pose: 0, lastTime: null },
    queueTime: 0,
    playing: false,
    events: [],
    micGeneration: 0,
    generation: 0, // bumped by stopAudio/dispose; invalidates pending queuePcm.
    waiters: [], // pending awaitPlayback resolvers.
    armed: false,
    clip: { recording: false, startMs: 0 },
    vad: freshVad(),
    windows: { enabled:false, chunks:[], samples:0, total:0, rate:24000, speaking:false, heard:false, node:null, source:null, sink:null, workletLoaded:false },
  };

  function freshVad() {
    return {
      epoch: 0,
      noise: 0.015,
      adaptive: true,
      sensitivity: 'balanced',
      noiseSamples: [],
      idleNoiseSamples: [],
      calibrationPending: true,
      calibratingUntil: null,
      lastIdleRecalibration: null,
      speaking: false,
      voiceSince: 0,
      lastVoice: 0,
      bargeSince: 0,
      barged: false,
      clipStart: 0,
      capFired: false,
      endSilenceMs: 1500,
      minSpeechMs: 300,
      bargeMs: BARGE_SUSTAIN_MS,
    };
  }

  function clamp(value, low, high) {
    return Math.max(low, Math.min(high, value));
  }

  function queueNoiseCalibration(vad) {
    vad.calibrationPending = true;
    vad.calibratingUntil = null;
    vad.lastIdleRecalibration = null;
    vad.noiseSamples = [];
    vad.idleNoiseSamples = [];
  }

  function percentileNoise(samples) {
    var sorted = samples.slice().sort(function (a, b) { return a - b; });
    return clamp(sorted[Math.floor(sorted.length * 0.60)], 0.002, 0.035);
  }

  // Adapted from Korina-Agent 9db02e6, Korina/js/vad.js.
  // Copyright (c) 2026 rgoza1996. MIT notice: KORINA_VAD_LICENSE.txt.
  // Keep only bounded RMS observations; never retain or export microphone PCM.
  // Kinaiya owns capture and playback separately from the prototype's UI state.
  function updateNoise(vad, level, now) {
    if (!vad.adaptive) return;
    var idle = !vad.speaking && !state.windows.speaking && !state.playing;
    var quiet = level < Math.max(vad.noise * 2.2 + 0.006, vad.noise * 2 + 0.008);
    if (!idle || (!quiet && !vad.calibrationPending && vad.calibratingUntil === null)) {
      vad.idleNoiseSamples = [];
      // Pause calibration once speech or local playback is active.
      if (vad.calibratingUntil !== null) queueNoiseCalibration(vad);
      return;
    }
    if (vad.calibrationPending) {
      vad.calibrationPending = false;
      vad.calibratingUntil = now + 900;
      vad.lastIdleRecalibration = now;
    }
    if (vad.calibratingUntil !== null && now < vad.calibratingUntil) {
      vad.noiseSamples.push(level);
      if (vad.noiseSamples.length > 240) vad.noiseSamples.shift();
      vad.noise = percentileNoise(vad.noiseSamples);
      return;
    }
    vad.calibratingUntil = null;
    vad.noiseSamples = [];
    vad.idleNoiseSamples.push(level);
    if (vad.idleNoiseSamples.length > 240) vad.idleNoiseSamples.shift();
    if (now - vad.lastIdleRecalibration >= 10000 && vad.idleNoiseSamples.length >= 30) {
      vad.noise = clamp(vad.noise * 0.70 + percentileNoise(vad.idleNoiseSamples) * 0.30, 0.002, 0.035);
      vad.idleNoiseSamples = [];
      vad.lastIdleRecalibration = now;
    }
  }

  function resetVadActivity(vad) {
    vad.epoch++;
    vad.speaking = false;
    vad.voiceSince = vad.lastVoice = vad.bargeSince = 0;
    vad.barged = false;
    vad.clipStart = nowMs();
    vad.capFired = false;
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

  function resetMouth() {
    state.mouth = { openness: 0, pose: 0, lastTime: null };
  }

  function pruneMouth(now) {
    state.mouthRecords = state.mouthRecords.filter(function (record) {
      return record.end > now && record.generation === state.generation;
    });
  }

  function mouthEnvelope(samples, rate) {
    var step = Math.max(1, Math.ceil(rate * MOUTH_WINDOW_SECONDS),
      Math.ceil(samples.length / MOUTH_MAX_WINDOWS));
    var levels = new Float32Array(Math.ceil(samples.length / step));
    for (var i = 0; i < levels.length; i++) {
      var end = Math.min(samples.length, (i + 1) * step);
      var sum = 0;
      for (var j = i * step; j < end; j++) sum += samples[j] * samples[j];
      levels[i] = Math.sqrt(sum / (end - i * step));
    }
    return { levels: levels, step: step / rate };
  }

  // Optional, bounded provider metadata. Invalid lists are discarded as a whole
  // so malformed timing cannot shift valid-looking cues onto different speech.
  function parseVisemes(input, duration) {
    if (typeof input === 'string') {
      if (input.length > 65536) return [];
      try { input = JSON.parse(input); } catch (e) { return []; }
    }
    if (!Array.isArray(input) || input.length > 256) return [];
    var result = [];
    var previousEnd = 0;
    for (var i = 0; i < input.length; i++) {
      var cue = input[i];
      if (!cue || typeof cue !== 'object' || Array.isArray(cue) ||
          typeof cue.start_ms !== 'number' || !Number.isFinite(cue.start_ms) ||
          typeof cue.end_ms !== 'number' || !Number.isFinite(cue.end_ms) ||
          cue.start_ms < previousEnd || cue.end_ms <= cue.start_ms ||
          cue.end_ms > duration * 1000 + 0.001 ||
          typeof cue.viseme !== 'string' ||
          !Object.prototype.hasOwnProperty.call(VISEME_OPENNESS, cue.viseme)) return [];
      result.push({ start: cue.start_ms / 1000, end: cue.end_ms / 1000, viseme: cue.viseme });
      previousEnd = cue.end_ms;
    }
    return result;
  }

  function readMouthFrame() {
    var closed = { openness: 0, viseme: null, active: false };
    if (!state.ctx || state.ctx.state !== 'running') {
      resetMouth();
      return closed;
    }
    var now = state.ctx.currentTime;
    pruneMouth(now);
    var record = null;
    for (var i = 0; i < state.mouthRecords.length; i++) {
      var candidate = state.mouthRecords[i];
      if (now >= candidate.start && now < candidate.end) { record = candidate; break; }
    }
    if (!record) { resetMouth(); return closed; }
    var local = now - record.start;
    var index = Math.min(record.levels.length - 1, Math.floor(local / record.step));
    var rms = record.levels[index];
    // A small silence gate closes immediately between words and keeps noise
    // from driving the mouth. Hysteresis below avoids rapid pose changes.
    if (rms < 0.008) {
      resetMouth();
      return { openness: 0, viseme: null, active: true };
    }
    var viseme = null;
    for (var j = 0; j < record.visemes.length; j++) {
      var cue = record.visemes[j];
      if (local >= cue.start && local < cue.end) { viseme = cue.viseme; break; }
    }
    var mouth = state.mouth;
    var target;
    if (viseme !== null) {
      target = VISEME_OPENNESS[viseme];
    } else {
      var pose = mouth.pose;
      if (rms >= (pose === 1 ? 0.055 : 0.075)) pose = 1;
      else if (rms >= (pose > 0 ? 0.008 : 0.012)) pose = 0.5;
      else pose = 0;
      mouth.pose = pose;
      target = pose;
    }
    var dt = mouth.lastTime === null ? 1 / 30 : Math.max(0, Math.min(0.1, now - mouth.lastTime));
    // Attack/release follow the playback clock, independent of UI poll rate.
    var tau = target > mouth.openness ? 0.025 : 0.045;
    mouth.openness += (target - mouth.openness) * (1 - Math.exp(-dt / tau));
    if (target === 0 && mouth.openness < 0.01) mouth.openness = 0;
    mouth.lastTime = now;
    return { openness: mouth.openness, viseme: viseme, active: true };
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

  function resetWindow() {
    state.windows.chunks=[];state.windows.samples=0;state.windows.total=0;
    state.windows.heard=false;
    state.windows.voices=0;state.windows.emittedVoices=-1;state.windows.emittedFinal=false;
  }

  function stopWindowCapture() {
    var w=state.windows;
    [w.node,w.source,w.sink].forEach(function(node){if(node){try{node.disconnect();}catch(e){}}});
    if(w.node && w.node.port) w.node.port.onmessage=null;
    if(w.node) w.node.onaudioprocess=null;
    w.node=w.source=w.sink=null;
    resetWindow();
  }

  function captureSamples(samples,rate) {
    var w=state.windows;
    if(!state.armed || !w.enabled || !samples || !samples.length)return;
    w.rate=rate;var copy=new Float32Array(samples.length);copy.set(samples);
    w.chunks.push(copy);w.samples+=copy.length;w.total+=copy.length;
    var cap=rate*30;
    while(w.samples>cap && w.chunks.length>1){w.samples-=w.chunks.shift().length;}
  }

  async function startWindowCapture() {
    stopWindowCapture();
    var w=state.windows,ctx=state.ctx;
    if(!w.enabled)return true;
    if(!ctx || !state.stream)return false;
    w.source=ctx.createMediaStreamSource(state.stream);
    w.sink=ctx.createGain();w.sink.gain.value=0;
    var Worklet=typeof AudioWorkletNode!=='undefined' ? AudioWorkletNode : window.AudioWorkletNode;
    if(ctx.audioWorklet && Worklet){
      try{
        if(!w.workletLoaded){await ctx.audioWorklet.addModule('window_capture_worklet.js');w.workletLoaded=true;}
        if(!state.armed || !state.stream)return false;
        w.node=new Worklet(ctx,'kinaiya-window-pcm');
        w.node.port.onmessage=function(e){captureSamples(e.data,ctx.sampleRate);};
      }catch(e){w.node=null;}
    }
    if(!w.node && typeof ctx.createScriptProcessor==='function'){
      w.node=ctx.createScriptProcessor(2048,1,1);
      w.node.onaudioprocess=function(e){captureSamples(e.inputBuffer.getChannelData(0),ctx.sampleRate);};
    }
    if(!w.node){stopWindowCapture();return false;}
    w.source.connect(w.node);w.node.connect(w.sink);w.sink.connect(ctx.destination);
    return true;
  }

  function windowWav() {
    var w=state.windows;
    if(!w.samples || !w.heard)return '';
    if(!w.speaking && w.emittedFinal && w.emittedVoices===w.voices)return '';
    w.emittedVoices=w.voices;w.emittedFinal=!w.speaking;
    var buffer=new ArrayBuffer(44+w.samples*2),view=new DataView(buffer);
    function str(pos,s){for(var i=0;i<s.length;i++)view.setUint8(pos+i,s.charCodeAt(i));}
    str(0,'RIFF');view.setUint32(4,36+w.samples*2,true);str(8,'WAVE');str(12,'fmt ');
    view.setUint32(16,16,true);view.setUint16(20,1,true);view.setUint16(22,1,true);
    view.setUint32(24,w.rate,true);view.setUint32(28,w.rate*2,true);view.setUint16(32,2,true);
    view.setUint16(34,16,true);str(36,'data');view.setUint32(40,w.samples*2,true);
    var offset=44;
    w.chunks.forEach(function(chunk){for(var i=0;i<chunk.length;i++){
      var value=Number.isFinite(chunk[i])?Math.max(-1,Math.min(1,chunk[i])):0;
      view.setInt16(offset,value<0?value*32768:value*32767,true);offset+=2;
    }});
    var bytes=new Uint8Array(buffer),binary='';
    for(var p=0;p<bytes.length;p+=8192)binary+=String.fromCharCode.apply(null,bytes.subarray(p,p+8192));
    return JSON.stringify({base64:btoa(binary),mime:'audio/wav',capture_epoch:state.vad.epoch,
      start_ms:Math.round((w.total-w.samples)*1000/w.rate),end_ms:Math.round(w.total*1000/w.rate),
      user_speaking:w.speaking,sample_rate:w.rate});
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

    queuePcm: function (base64, sampleRate, visemes) {
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
        pruneMouth(ctx.currentTime);
        // Retain the next audible chunks when an unusually deep queue fills
        // the metadata budget; playback acknowledgments remain independent.
        var mouthRecord = null;
        if (state.mouthRecords.length < MOUTH_MAX_CHUNKS) {
          var envelope = mouthEnvelope(ch, rate);
          mouthRecord = {
            start: rec.start, end: rec.end, generation: gen, source: src,
            levels: envelope.levels, step: envelope.step,
            visemes: parseVisemes(visemes, buf.duration),
          };
          state.mouthRecords.push(mouthRecord);
        }
        state.queueTime = rec.end;
        state.sources.push(src);
        state.records.push(rec);
        if (state.records.length > 128) state.records.shift();
        src.onended = function () {
          if (gen !== state.generation) return;
          // Look up by source so expired envelope metadata is not retained by
          // this callback while waiting for a delayed browser onended event.
          state.mouthRecords = state.mouthRecords.filter(function (r) {
            return r.source !== src;
          });
          var i = state.sources.indexOf(src);
          if (i >= 0) state.sources.splice(i, 1);
          if (state.sources.length === 0) {
            state.mouthRecords = [];
            resetMouth();
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

    queuePcmWithVisemes: function (base64, sampleRate, visemes) {
      return api.queuePcm(base64, sampleRate, visemes);
    },

    mouthFrame: function () {
      return JSON.stringify(readMouthFrame());
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
      state.mouthRecords = [];
      resetMouth();
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
      stopWindowCapture();
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
      if (typeof cfg !== 'object' || Array.isArray(cfg) ||
          Object.keys(cfg).some(function (key) {
            return ['adaptive', 'sensitivity', 'endSilenceMs', 'minSpeechMs', 'bargeMs'].indexOf(key) < 0;
          }) ||
          ('adaptive' in cfg && typeof cfg.adaptive !== 'boolean') ||
          ('sensitivity' in cfg && ['low', 'balanced', 'high'].indexOf(cfg.sensitivity) < 0) ||
          ['endSilenceMs', 'minSpeechMs', 'bargeMs'].some(function (key) {
            return key in cfg && (typeof cfg[key] !== 'number' || !Number.isFinite(cfg[key]));
          })) return 'invalid';
      var vad = state.vad;
      if ('endSilenceMs' in cfg) vad.endSilenceMs = clamp(Math.round(cfg.endSilenceMs), 400, 5000);
      if ('minSpeechMs' in cfg) vad.minSpeechMs = clamp(Math.round(cfg.minSpeechMs), 100, 2000);
      if ('bargeMs' in cfg) vad.bargeMs = clamp(Math.round(cfg.bargeMs), 100, 2000);
      if ('sensitivity' in cfg) vad.sensitivity = cfg.sensitivity;
      if ('adaptive' in cfg && cfg.adaptive !== vad.adaptive) {
        vad.adaptive = cfg.adaptive;
        queueNoiseCalibration(vad);
      }
      if (Object.keys(cfg).length) {
        // A changed threshold must earn fresh speech/barge confirmation.
        vad.voiceSince = vad.bargeSince = 0;
      }
      return JSON.stringify({
        schemaVersion: 1,
        adaptive: vad.adaptive,
        sensitivity: vad.sensitivity,
        endSilenceMs: vad.endSilenceMs,
        minSpeechMs: vad.minSpeechMs,
        bargeMs: vad.bargeMs,
        clipCapMs: CLIP_CAP_MS,
      });
    },

    // Queues calibration for quiet local samples; never starts a microphone,
    // stops playback, resets a capture epoch, or changes a conversation turn.
    vadRecalibrate: function () {
      if (!state.vad.adaptive) return 'unavailable';
      queueNoiseCalibration(state.vad);
      return 'queued';
    },

    vadSample: function (rms, tMs, epoch) {
      var now = typeof tMs === 'number' ? tMs : nowMs();
      if (typeof epoch === 'number' && epoch !== state.vad.epoch) return '';
      if (!state.armed) return '';
      if (typeof rms !== 'number' || !Number.isFinite(rms) || !Number.isFinite(now)) return '';
      var r = clamp(rms, 0, 1);
      var vad = state.vad;
      // Distinct speech/silence thresholds prevent noisy state flapping.
      updateNoise(vad, r, now);
      var factor = vad.sensitivity === 'high' ? 0.75 : vad.sensitivity === 'low' ? 1.25 : 1;
      var silence = (vad.adaptive ? clamp(vad.noise * 2.2 + 0.006, 0.012, 0.045) : 0.022) * factor;
      var speech = (vad.adaptive ? clamp(Math.max(silence / factor + 0.010, vad.noise * 3.4 + 0.012), 0.026, 0.085) : 0.035) * factor;
      var voice = r > speech || (vad.speaking && r >= silence);
      if(state.windows.enabled){
        state.windows.speaking=voice;
        if(voice){state.windows.heard=true;state.windows.voices++;}
        return '';
      }
      // Barge timing is independent of ordinary turn-start confirmation.
      if (state.playing && !vad.barged) {
        if (r > speech) {
          if (!vad.bargeSince) vad.bargeSince = now;
          if (now - vad.bargeSince >= vad.bargeMs) {
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
          resetVadActivity(state.vad);
          queueNoiseCalibration(state.vad);
          pumpStart();
          if(state.windows.enabled && !(await startWindowCapture())){
            state.armed=false;pumpStop();stopTracks();return 'unavailable';
          }
          if(micOwner!==state.micGeneration){stopWindowCapture();stopTracks();return 'unavailable';}
          pushEvent({ type: 'armed', active: true, epoch: state.vad.epoch });
          return 'armed';
        },
        function () {
          return 'unavailable';
        },
      );
    },

    stopArmed: function () {
      stopWindowCapture();
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
      if(state.windows.enabled)return Promise.resolve('window-capture');
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
      resetVadActivity(state.vad);
      state.clip = { recording: true, startMs: nowMs() };
      return Promise.resolve('clip-recording');
    },

    captureEpoch: function () { return String(state.vad.epoch); },

    windowConfigure: function(enabled){
      state.windows.enabled=enabled===true;
      if(!state.windows.enabled)stopWindowCapture();
      return state.windows.enabled?'windows':'legacy';
    },
    windowSnapshot: function(){return windowWav();},
    windowSpeaking: function(){return state.windows.speaking?'yes':'no';},
    nextWindowUtterance: function(){resetWindow();state.vad.epoch++;return String(state.vad.epoch);},

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
