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
    micAcquire: null,
    micStop: null,
    generation: 0, // bumped by stopAudio/dispose; invalidates pending queuePcm.
    output: { revision: 0, listing: 0, devices: [], selected: null, failed: false, pending: null, watching: false },
    input: { revision: 0, devices: [], selected: null, failed: false, pending: null, watching: false },
    nativePcm: { enabled: false, ctx: null, source: null, node: null, sink: null, closing: null, failed: false, recording: false, heard: false, overflow: false, chunks: [], samples: 0, pre: [], preSamples: 0 },
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

  function audioContext(sampleRate) {
    if (!state.ctx) {
      var AC =
        typeof window !== 'undefined' &&
        (window.AudioContext || window.webkitAudioContext);
      if (!AC) throw new Error('Web audio unavailable');
      state.ctx = new AC({ sampleRate: sampleRate || 24000 });
      var original = state.ctx;
      if (typeof original.addEventListener === 'function') {
        original.addEventListener('sinkchange', function () {
          if (state.ctx === original && !state.output.pending && !outputMatches(original)) {
            failOutput();
          }
        });
      }
    }
    return state.ctx;
  }

  function ensureContext(sampleRate) {
    try { audioContext(sampleRate); }
    catch (e) { return Promise.reject(e); }
    if (state.ctx.state === 'suspended') {
      return state.ctx.resume().then(function () {
        return state.ctx;
      });
    }
    return Promise.resolve(state.ctx);
  }

  function outputEnvironment() {
    var nav = typeof navigator !== 'undefined' ? navigator : window && window.navigator;
    var AC = typeof window !== 'undefined' && (window.AudioContext || window.webkitAudioContext);
    var sink = state.ctx ? typeof state.ctx.setSinkId === 'function' :
      !!(AC && AC.prototype && typeof AC.prototype.setSinkId === 'function');
    return { media: nav && nav.mediaDevices,
      supported: !!(sink && nav && nav.mediaDevices &&
        typeof nav.mediaDevices.enumerateDevices === 'function' && window.isSecureContext !== false),
      picker: !!(nav && nav.mediaDevices && typeof nav.mediaDevices.selectAudioOutput === 'function') };
  }

  function explicitOutput() {
    return state.output.selected !== null && state.output.selected !== '';
  }

  function outputMatches(ctx) {
    if (state.output.failed) return false;
    return state.output.selected === null ||
      (typeof ctx.sinkId === 'string' && ctx.sinkId === state.output.selected);
  }

  function failOutput() {
    state.output.failed = true;
    // This is a playback stop request, not a physical delivery acknowledgment.
    api.stopAudio();
    pushEvent({ type: 'audio_output', status: 'unavailable' });
  }

  function watchOutputs(media) {
    if (state.output.watching || !media || typeof media.addEventListener !== 'function') return;
    state.output.watching = true;
    media.addEventListener('devicechange', function () {
      // A topology change needs a fresh explicit choice. Never silently fall
      // back from an explicitly selected recording/speaker device.
      if (explicitOutput() || state.output.pending) failOutput();
      state.output.devices = [];
    });
  }

  function checkedOutputs(raw) {
    if (!Array.isArray(raw) || raw.length > 128) throw new Error('Output devices unavailable');
    var result = [], ids = new Set();
    for (var i = 0; i < raw.length; i++) {
      var device = raw[i];
      if (!device || device.kind !== 'audiooutput') continue;
      if (typeof device.deviceId !== 'string' || !device.deviceId.length || device.deviceId.length > 4096 ||
          typeof device.label !== 'string' || device.label.length > 4096 || ids.has(device.deviceId)) {
        throw new Error('Output devices unavailable');
      }
      ids.add(device.deviceId);
      result.push({ id: device.deviceId, label: device.label || 'Audio output' });
    }
    return result;
  }

  function outputListing(status) {
    var env = outputEnvironment(), out = state.output;
    return JSON.stringify({ status: status, supported: env.supported, picker: env.supported && env.picker,
      pending: !!out.pending, explicit: explicitOutput(), failed: out.failed,
      devices: out.devices.map(function (device) {
        return { id: device.id, label: device.label,
          selected: !out.failed && !!state.ctx && outputMatches(state.ctx) && out.selected === device.id };
      }) });
  }

  function listOutputs() {
    var env = outputEnvironment(), out = state.output;
    if (!env.supported) {
      out.devices = [];
      if (explicitOutput()) failOutput();
      return Promise.resolve(outputListing('unavailable'));
    }
    watchOutputs(env.media);
    var listing = ++out.listing, revision = out.revision;
    return Promise.resolve().then(function () { return env.media.enumerateDevices(); }).then(function (raw) {
      var devices = checkedOutputs(raw);
      if (listing !== out.listing || revision !== out.revision) return outputListing('stopped');
      out.devices = devices;
      if (!out.pending && explicitOutput() && (!devices.some(function (d) { return d.id === out.selected; }) ||
          !state.ctx || !outputMatches(state.ctx))) failOutput();
      return outputListing('ready');
    }).catch(function () {
      if (listing === out.listing && revision === out.revision) {
        out.devices = [];
        if (explicitOutput()) failOutput();
      }
      return outputListing('unavailable');
    });
  }

  function outputResult(status) { return JSON.stringify({ status: status }); }

  function changeOutput(kind, id) {
    var out = state.output, env = outputEnvironment();
    if (out.pending || state.sources.length) return Promise.resolve(outputResult('busy'));
    if (!env.supported || (kind === 'choose' && !env.picker)) return Promise.resolve(outputResult('unavailable'));
    if (kind === 'select' && (typeof id !== 'string' || !id.length || id.length > 4096)) return Promise.resolve(outputResult('invalid'));
    var ctx;
    try { ctx = audioContext(24000); }
    catch (e) { return Promise.resolve(outputResult('unavailable')); }
    if (typeof ctx.setSinkId !== 'function' || typeof ctx.sinkId !== 'string') return Promise.resolve(outputResult('unavailable'));
    watchOutputs(env.media);
    var op = { revision: ++out.revision, generation: state.generation, ctx: ctx,
      dispatched: false, promise: null };
    out.pending = op;
    ++out.listing; // Older enumeration cannot replace this choice's snapshot.
    function current() {
      return out.pending === op && out.revision === op.revision && state.generation === op.generation && state.ctx === ctx;
    }
    var chosen;
    try {
      // Invoke the browser picker in the owner's original gesture, before an
      // await. Device IDs are never accepted from a server or persisted.
      chosen = kind === 'choose' ? env.media.selectAudioOutput() : Promise.resolve(null);
    } catch (e) { chosen = Promise.reject(e); }
    op.promise = Promise.resolve(chosen).then(function (device) {
      if (!current()) return 'stopped';
      if (kind === 'default') return '';
      if (kind === 'choose') {
        if (!device || device.kind !== 'audiooutput' || typeof device.deviceId !== 'string' ||
            !device.deviceId.length || device.deviceId.length > 4096) return null;
        id = device.deviceId;
      }
      return Promise.resolve(env.media.enumerateDevices()).then(function (raw) {
        var devices = checkedOutputs(raw);
        if (!current()) return null;
        out.devices = devices;
        return devices.some(function (d) { return d.id === id; }) ? id : null;
      });
    }).then(function (target) {
      if (!current()) return 'stopped';
      if (target === null) return 'invalid';
      // Fresh native discovery established this intent. A failed/mismatched
      // setSinkId must retain it as failed, never fall back to the old route.
      out.selected = target;
      op.dispatched = true;
      return Promise.resolve(ctx.setSinkId(target)).then(function () {
        if (!current()) {
          if (state.ctx === ctx) out.failed = true;
          return 'stopped';
        }
        if (ctx.sinkId !== target) { out.failed = true; return 'failed'; }
        out.selected = target;
        out.failed = false;
        return target === '' ? 'default' : 'selected';
      });
    }).catch(function (error) {
      if (state.ctx === ctx && op.dispatched) out.failed = true;
      if (!current()) return 'stopped';
      if (!op.dispatched && kind === 'choose' && error &&
          (error.name === 'AbortError' || error.name === 'NotAllowedError')) return 'cancelled';
      return op.dispatched ? 'failed' : 'unavailable';
    }).then(function (status) {
      // A held browser operation retains its slot through stop/dispose. A
      // timeout in an observing UI cannot authorize a replacement operation.
      if (out.pending === op) out.pending = null;
      return outputResult(status);
    });
    return op.promise;
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
    stopNativePcm().catch(function () {});
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

  function inputEnvironment() {
    var nav = typeof navigator !== 'undefined' ? navigator : window && window.navigator;
    return nav && nav.mediaDevices && typeof nav.mediaDevices.enumerateDevices === 'function'
      ? nav.mediaDevices : null;
  }

  function inputBusy() {
    return !!(state.micAcquire || state.micStop || state.nativePcm.closing || state.stream || state.armed || state.recorder);
  }

  var inputIdentifiersUnavailable = {};

  function inputRows(raw) {
    if (!Array.isArray(raw) || raw.length > 128) throw new Error('Input unavailable');
    var rows = [], ids = new Set(), identifiersUnavailable = false;
    raw.forEach(function (device) {
      if (!device || device.kind !== 'audioinput') return;
      if (typeof device.deviceId !== 'string' || device.deviceId.length > 4096 ||
          typeof device.label !== 'string' || device.label.length > 4096) {
        throw new Error('Input unavailable');
      }
      if (!device.deviceId.length) { identifiersUnavailable = true; return; }
      if (ids.has(device.deviceId)) throw new Error('Input unavailable');
      ids.add(device.deviceId);
      rows.push({ id: device.deviceId, label: device.label || 'Audio input' });
    });
    // Anonymous rows cannot be selected, but do not invalidate exposed IDs.
    // A private sentinel distinguishes an entirely hidden inventory from errors.
    if (identifiersUnavailable && !rows.length) throw inputIdentifiersUnavailable;
    return rows;
  }

  function inputLost() {
    state.input.revision++;
    state.input.failed = true;
    if (state.armed || state.micAcquire) {
      api.stopArmed().catch(function () {});
      pushEvent({ type: 'native-input-unavailable', epoch: state.vad.epoch });
    }
  }

  function watchInput(media) {
    if (state.input.watching || typeof media.addEventListener !== 'function') return;
    state.input.watching = true;
    media.addEventListener('devicechange', function () {
      if (state.input.selected !== null) inputLost();
    });
  }

  function listInputs() {
    var input = state.input, media = inputEnvironment();
    if (input.pending) {
      if (input.pending.kind === 'listing') return input.pending.promise;
      return Promise.resolve(JSON.stringify({ supported: !!media, pending: true,
        explicit: input.selected !== null, failed: input.failed, devices: [], status: 'unavailable' }));
    }
    var op = { promise: null, kind: 'listing' }, revision = input.revision, reason = null;
    function unavailable(error) {
      if (input.selected !== null) inputLost();
      if (error === inputIdentifiersUnavailable) reason = 'identifiers-unavailable';
      return false;
    }
    input.pending = op;
    op.promise = Promise.resolve().then(function () {
      if (!media) throw new Error('Input unavailable');
      watchInput(media);
      return media.enumerateDevices();
    }).then(function (raw) {
      if (revision !== input.revision) throw new Error('Input unavailable');
      input.devices = inputRows(raw);
      if (input.selected !== null && !input.devices.some(function (d) { return d.id === input.selected; })) inputLost();
      return !input.failed;
    }, unavailable)
      .catch(unavailable)
      .then(function (ready) {
        if (input.pending === op) input.pending = null;
        var result = { supported: !!media, pending: false, explicit: input.selected !== null,
          failed: input.failed, status: ready ? 'ready' : 'unavailable',
          devices: ready ? input.devices.map(function (d) { return { id: d.id, label: d.label, selected: d.id === input.selected }; }) : [] };
        if (reason !== null) result.reason = reason;
        return JSON.stringify(result);
      });
    return op.promise;
  }

  function changeInput(id, useDefault) {
    var input = state.input, media = inputEnvironment();
    if (input.pending || inputBusy()) return Promise.resolve(outputResult('busy'));
    if (!media) return Promise.resolve(outputResult('unavailable'));
    if (!useDefault && (typeof id !== 'string' || !id.length || id.length > 4096)) return Promise.resolve(outputResult('invalid'));
    if (useDefault) {
      input.revision++; input.selected = null; input.failed = false;
      return Promise.resolve(outputResult('default'));
    }
    var op = { promise: null, kind: 'selection' }, revision = input.revision;
    input.pending = op;
    op.promise = Promise.resolve().then(function () { return media.enumerateDevices(); }).then(function (raw) {
      var rows = inputRows(raw);
      if (revision !== input.revision || inputBusy()) return 'busy';
      if (!rows.some(function (d) { return d.id === id; })) return 'invalid';
      input.devices = rows; input.selected = id; input.failed = false; input.revision++;
      watchInput(media);
      return 'selected';
    }).catch(function () { return 'unavailable'; }).then(function (status) {
      if (input.pending === op) input.pending = null;
      return outputResult(status);
    });
    return op.promise;
  }

  function micConstraints(selected) {
    var audio = { echoCancellation: true, noiseSuppression: true, autoGainControl: true };
    if (state.nativePcm.enabled) audio.channelCount = 1;
    if (selected !== null) audio.deviceId = { exact: selected };
    return { audio: audio };
  }

  function stopStream(stream) {
    try { stream.getTracks().forEach(function (track) { track.stop(); }); } catch (e) {}
  }

  function acquireMic(start) {
    var nav = typeof navigator !== 'undefined' ? navigator : window && window.navigator;
    if (state.micAcquire || state.micStop || state.input.pending) return Promise.resolve('busy');
    if (!nav || !nav.mediaDevices || typeof nav.mediaDevices.getUserMedia !== 'function' ||
        (state.input.selected !== null && state.input.failed)) return Promise.resolve('unavailable');
    var owner = ++state.micGeneration, selected = state.input.selected, revision = state.input.revision;
    var op = { promise: null }, original;
    try { original = nav.mediaDevices.getUserMedia(micConstraints(selected)); }
    catch (e) { return Promise.resolve('unavailable'); }
    state.micAcquire = op;
    op.promise = Promise.resolve(original).then(async function (stream) {
      if (owner !== state.micGeneration || revision !== state.input.revision) { stopStream(stream); return 'unavailable'; }
      if (selected !== null) {
        var tracks = typeof stream.getAudioTracks === 'function' ? stream.getAudioTracks() : [];
        var settings = tracks.length === 1 && typeof tracks[0].getSettings === 'function' ? tracks[0].getSettings() : null;
        if (!settings || settings.deviceId !== selected) { state.input.failed = true; stopStream(stream); return 'unavailable'; }
      }
      state.stream = stream;
      try {
        var result = await start(owner);
        if (owner !== state.micGeneration) { stopStream(stream); return 'unavailable'; }
        return result;
      } catch (e) {
        if (state.stream === stream) stopTracks(); else stopStream(stream);
        state.armed = false;
        return 'unavailable';
      }
    }, function () { return 'unavailable'; }).then(function (status) {
      if (state.micAcquire === op) state.micAcquire = null;
      return status;
    });
    return op.promise;
  }

  function resetNativeClip() {
    var n = state.nativePcm;
    n.recording = false; n.heard = false; n.overflow = false; n.chunks = []; n.samples = 0;
  }

  function stopNativePcm() {
    var n = state.nativePcm;
    if (n.closing) return n.closing;
    [n.node, n.source, n.sink].forEach(function (node) { if (node) { try { node.disconnect(); } catch (e) {} } });
    if (n.node && n.node.port) n.node.port.onmessage = null;
    if (n.node) n.node.onaudioprocess = null;
    n.node = n.source = n.sink = null;
    resetNativeClip(); n.pre = []; n.preSamples = 0;
    var ctx = n.ctx; n.ctx = null;
    if (!ctx) return Promise.resolve();
    var original;
    try { original = typeof ctx.close === 'function' ? ctx.close() : Promise.resolve(); }
    catch (e) { original = Promise.reject(e); }
    n.closing = Promise.resolve(original).then(function () { n.closing = null; }, function () {
      n.failed = true; throw new Error('Native capture cleanup unavailable');
    });
    return n.closing;
  }

  function nativeSamples(samples) {
    var n = state.nativePcm;
    if (!state.armed || !n.enabled || n.failed || n.overflow || !samples || !samples.length) return;
    if (!Number.isInteger(samples.length) || samples.length > 16384) {
      n.failed = true; resetNativeClip();
      pushEvent({ type: 'native-capture-unavailable', epoch: state.vad.epoch });
      return;
    }
    var copy = new Float32Array(samples.length); copy.set(samples);
    for (var i = 0; i < copy.length; i++) {
      if (!Number.isFinite(copy[i])) {
        n.failed = true; resetNativeClip();
        pushEvent({ type: 'native-capture-unavailable', epoch: state.vad.epoch });
        return;
      }
    }
    if (!n.recording || !n.heard) {
      n.pre.push(copy); n.preSamples += copy.length;
      while (n.preSamples > 40000 && n.pre.length) {
        var excess = n.preSamples - 40000, head = n.pre[0];
        if (head.length <= excess) { n.pre.shift(); n.preSamples -= head.length; }
        else { n.pre[0] = head.slice(excess); n.preSamples -= excess; }
      }
      return;
    }
    if (n.samples + copy.length > 480000) {
      n.overflow = true; n.recording = false; n.chunks = []; n.samples = 0;
      pushEvent({ type: 'native-clip-overflow', epoch: state.vad.epoch });
      return;
    }
    n.chunks.push(copy); n.samples += copy.length;
  }

  function nativeSpeechStart() {
    var n = state.nativePcm;
    if (!n.enabled || !n.recording || n.heard || n.overflow || n.failed) return;
    n.heard = true; n.chunks = n.pre.slice(); n.samples = n.preSamples;
    n.pre = []; n.preSamples = 0;
  }

  async function startNativePcm(owner) {
    var n = state.nativePcm, AC = window.AudioContext || window.webkitAudioContext;
    if (!AC || n.failed || n.closing) throw new Error('Native capture unavailable');
    var ctx = new AC({ sampleRate: 16000 }); n.ctx = ctx;
    if (ctx.sampleRate !== 16000) throw new Error('Native capture rate unavailable');
    if (ctx.state === 'suspended') await ctx.resume();
    if (owner !== state.micGeneration || ctx !== n.ctx || ctx.state !== 'running') throw new Error('Native capture stopped');
    n.source = ctx.createMediaStreamSource(state.stream);
    n.sink = ctx.createGain(); n.sink.gain.value = 0;
    var Worklet = typeof AudioWorkletNode !== 'undefined' ? AudioWorkletNode : window.AudioWorkletNode;
    if (ctx.audioWorklet && Worklet) {
      try {
        await ctx.audioWorklet.addModule('window_capture_worklet.js');
        if (owner !== state.micGeneration || ctx !== n.ctx) throw new Error('Native capture stopped');
        n.node = new Worklet(ctx, 'kinaiya-window-pcm', {
          numberOfInputs: 1, numberOfOutputs: 1, channelCount: 1,
          channelCountMode: 'explicit', channelInterpretation: 'speakers',
        });
        n.node.port.onmessage = function (e) { if (owner === state.micGeneration && ctx === n.ctx) nativeSamples(e.data); };
      } catch (e) { if (owner !== state.micGeneration || ctx !== n.ctx) throw e; }
    }
    if (!n.node && typeof ctx.createScriptProcessor === 'function') {
      n.node = ctx.createScriptProcessor(2048, 1, 1);
      n.node.onaudioprocess = function (e) {
        if (owner === state.micGeneration && ctx === n.ctx) nativeSamples(e.inputBuffer.getChannelData(0));
      };
    }
    if (!n.node) throw new Error('Native capture unavailable');
    n.source.connect(n.node); n.node.connect(n.sink); n.sink.connect(ctx.destination);
  }

  function finishNativeClip() {
    var n = state.nativePcm;
    if (n.overflow || n.failed) {
      var refusal = n.overflow ? 'overflow' : 'unavailable';
      resetNativeClip();
      return JSON.stringify({ mime: 'audio/wav', base64: '', native_error: refusal });
    }
    if (!n.recording || !n.heard || !n.samples || n.samples > 480000) {
      resetNativeClip(); return JSON.stringify({ mime: 'audio/wav', base64: '' });
    }
    var buffer = new ArrayBuffer(44 + n.samples * 2), view = new DataView(buffer);
    function str(pos, value) { for (var i = 0; i < value.length; i++) view.setUint8(pos + i, value.charCodeAt(i)); }
    str(0, 'RIFF'); view.setUint32(4, 36 + n.samples * 2, true); str(8, 'WAVE'); str(12, 'fmt ');
    view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
    view.setUint32(24, 16000, true); view.setUint32(28, 32000, true); view.setUint16(32, 2, true);
    view.setUint16(34, 16, true); str(36, 'data'); view.setUint32(40, n.samples * 2, true);
    var offset = 44;
    n.chunks.forEach(function (chunk) { for (var i = 0; i < chunk.length; i++) {
      var value = clamp(chunk[i], -1, 1); view.setInt16(offset, value < 0 ? value * 32768 : value * 32767, true); offset += 2;
    } });
    var bytes = new Uint8Array(buffer), binary = '';
    for (var p = 0; p < bytes.length; p += 8192) binary += String.fromCharCode.apply(null, bytes.subarray(p, p + 8192));
    resetNativeClip();
    return JSON.stringify({ mime: 'audio/wav', base64: btoa(binary), bytes: bytes.length });
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
      var captureContext = state.nativePcm.enabled ? state.nativePcm.ctx : state.ctx;
      if (!captureContext || !state.stream) return;
      if (typeof captureContext.createAnalyser !== 'function') return;
      if (typeof window === 'undefined') return;
      var src = captureContext.createMediaStreamSource(state.stream);
      state.analyser = captureContext.createAnalyser();
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
    inputDevices: listInputs,
    selectInput: function (id) { return changeInput(id, false); },
    defaultInput: function () { return changeInput(null, true); },
    nativePcmConfigure: function (enabled) {
      var n = state.nativePcm;
      if (typeof enabled !== 'boolean') return 'unavailable';
      if (inputBusy() || n.closing) return 'busy';
      if (enabled && (n.failed || typeof window === 'undefined' || !(window.AudioContext || window.webkitAudioContext))) return 'unavailable';
      n.enabled = enabled;
      return enabled ? 'native-pcm' : 'legacy';
    },
    outputDevices: listOutputs,
    selectOutput: function (id) { return changeOutput('select', id); },
    chooseOutput: function () { return changeOutput('choose'); },
    defaultOutput: function () { return changeOutput('default'); },
    chooseRecording: function () {
      // Resume playback in the import gesture, before the file picker and
      // asynchronous upload consume browser activation. This opens no mic or
      // audio source, and never delays the picker on a held resume promise.
      try {
        ensureContext(24000).catch(function () { /* Playback remains unavailable. */ });
      } catch (e) { /* Import is still available on hosts without Web Audio. */ }
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
      api.stopAudio();
      return acquireMic(function () {
          startRecorderOnStream();
          pushEvent({ type: 'recording', active: true });
          return 'recording';
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
      var outputRevision = state.output.revision;
      if (state.output.pending || state.output.failed) return Promise.resolve('stopped');
      return ensureContext(sampleRate || 24000).then(function (ctx) {
        if (gen !== state.generation || outputRevision !== state.output.revision || ctx.state !== 'running' || state.output.pending || !outputMatches(ctx)) return 'stopped';
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
        if (gen !== state.generation || outputRevision !== state.output.revision || state.ctx !== ctx || state.output.pending || !outputMatches(ctx)) return 'stopped';
        var src = ctx.createBufferSource();
        src.buffer = buf;
        src.connect(ctx.destination);
        var start = Math.max(ctx.currentTime, state.queueTime);
        if (gen !== state.generation || outputRevision !== state.output.revision || state.ctx !== ctx || state.output.pending || !outputMatches(ctx)) {
          try { src.disconnect(); } catch (e) { /* Never started. */ }
          return 'stopped';
        }
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
      state.input.revision++;
      state.input.devices = [];
      state.input.selected = null;
      state.input.failed = false;
      state.output.revision++;
      state.output.listing++;
      state.output.devices = [];
      state.output.selected = null;
      state.output.failed = false;
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
            nativeSpeechStart();
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
        !state.nativePcm.enabled &&
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
      api.stopAudio();
      if (state.armed) return Promise.resolve('armed');
      return acquireMic(async function (micOwner) {
          await ensureContext(24000);
          if (micOwner !== state.micGeneration) throw new Error('Microphone stopped');
          state.armed = true;
          resetVadActivity(state.vad);
          queueNoiseCalibration(state.vad);
          if (state.nativePcm.enabled) await startNativePcm(micOwner);
          if (micOwner !== state.micGeneration) throw new Error('Microphone stopped');
          pumpStart();
          if(state.windows.enabled && !(await startWindowCapture())){
            state.armed=false;pumpStop();stopTracks();return 'unavailable';
          }
          if(micOwner!==state.micGeneration){stopWindowCapture();stopTracks();return 'unavailable';}
          pushEvent({ type: 'armed', active: true, epoch: state.vad.epoch });
          return 'armed';
      });
    },

    stopArmed: function () {
      if (state.micStop) return state.micStop.promise;
      var acquisition = state.micAcquire && state.micAcquire.promise;
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
      var op = { promise: null };
      state.micStop = op;
      op.promise = Promise.all([
        acquisition ? Promise.resolve(acquisition).then(function () {}) : Promise.resolve(),
        state.nativePcm.closing || Promise.resolve(),
      ]).then(function () {
        if (state.micStop === op) state.micStop = null;
        return 'idle';
      });
      return op.promise;
    },

    beginClip: function () {
      if (state.nativePcm.enabled) {
        var n = state.nativePcm;
        if (!state.armed || !state.stream || n.failed || n.closing) return Promise.resolve('not-armed');
        if (n.recording) return Promise.resolve('busy');
        resetNativeClip(); n.recording = true;
        resetVadActivity(state.vad);
        return Promise.resolve('clip-recording');
      }
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
      if (enabled === true && state.nativePcm.enabled) return 'unavailable';
      state.windows.enabled=enabled===true;
      if(!state.windows.enabled)stopWindowCapture();
      return state.windows.enabled?'windows':'legacy';
    },
    windowSnapshot: function(){return windowWav();},
    windowSpeaking: function(){return state.windows.speaking?'yes':'no';},
    nextWindowUtterance: function(){resetWindow();state.vad.epoch++;return String(state.vad.epoch);},

    finishClip: function () {
      if (state.nativePcm.enabled) return Promise.resolve(finishNativeClip());
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
