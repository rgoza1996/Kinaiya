// #623: contiguous bounded PCM across real bridge entrypoints. Synthetic public
// WebAudio fixtures establish protocol/ownership, not physical microphone proof.
'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {fixture, gate} = require('./native_call_input_selection.test.js');

function state(f) { return JSON.parse(f.api.streamPcmState()); }
function packet(f) {
  const raw=f.api.streamPcmPeek();
  return raw==='' ? null : JSON.parse(raw);
}
function original(p) {
  return {capture_epoch:p.capture_epoch,sequence:p.sequence,
    sample_offset:p.sample_offset,sample_count:p.sample_count};
}
function acknowledge(f,p) { return f.api.streamPcmAck(JSON.stringify(original(p))); }
async function armed(options={}) {
  const f=fixture(options);
  assert.equal(typeof f.api.streamPcmConfigure,'function');
  assert.equal(f.api.streamPcmConfigure(true),'stream-pcm');
  assert.equal(f.seen.gets.length,0,'configuration cannot acquire a microphone');
  assert.equal(await f.api.startArmed(),'armed');
  return f;
}

for (const ending of ['stop', 'natural']) {
  test(`backchannel ${ending} keeps speaker PCM outside the mic FIFO and preserves live input`,async()=>{
    const f=await armed(),epoch=Number(f.api.captureEpoch());
    assert.equal(f.seen.gets[0].audio.echoCancellation,true);
    assert.equal(f.seen.gets[0].audio.noiseSuppression,true);
    const speaker=Buffer.from([16,0,32,0,48,0,64,0]).toString('base64');
    assert.equal(await f.api.queuePcm(speaker,24000),'queued');
    assert.equal(f.api.playbackActive(),'yes');
    assert.equal(f.seen.gets.length,1);
    assert.equal(state(f).received_samples,0,'local speaker output is never appended as mic input');
    assert.equal(packet(f),null);
    let inputReady=false,playbackEnded=false;
    const input=f.api.streamPcmWait(String(epoch)).then(()=>{inputReady=true;});
    const drain=f.api.awaitPlayback().then(()=>{playbackEnded=true;});
    await Promise.resolve();
    assert.equal(inputReady,false,'speaker output cannot wake captured-input readiness');
    assert.equal(playbackEnded,false,'queued PCM is not a completed local source');
    f.feed(new Float32Array([0.25,-0.25]));
    await input;
    const first=packet(f);
    assert.equal(first.capture_epoch,epoch);
    assert.equal(first.sample_offset,0);
    assert.equal(first.sample_count,2);
    assert.deepEqual([0,2].map(i=>Buffer.from(first.pcm16_base64,'base64').readInt16LE(i)),[8191,-8192]);
    if (ending==='stop') assert.equal(f.api.stopAudio(),'stopped');
    else {
      assert.equal(f.seen.sources.length,1);
      f.seen.sources[0].onended();
    }
    await drain;
    assert.equal(f.api.playbackActive(),'no');
    assert.equal(state(f).active,true,'speaker completion cannot mute the mic');
    assert.equal(Number(f.api.captureEpoch()),epoch);
    assert.deepEqual(packet(f),first);
    f.feed(new Float32Array([0.5]));
    assert.equal(acknowledge(f,first),'confirmed');
    const next=packet(f);
    assert.equal(next.capture_epoch,epoch);
    assert.equal(next.sequence,2);
    assert.equal(next.sample_offset,2);
    assert.equal(next.sample_count,1);
    assert.equal(Buffer.from(next.pcm16_base64,'base64').readInt16LE(0),16383);
    assert.equal(f.seen.gets.length,1);
    assert.equal(acknowledge(f,next),'confirmed');
    await f.api.stopArmed();
    assert.equal(f.api.streamPcmRelease(String(epoch)),'released');
  });
}

test('fresh speaking is independent of a retained packet and legacy windows',async()=>{
  const f=await armed(),epoch=Number(f.api.captureEpoch());
  assert.equal(typeof f.api.streamPcmSpeaking,'function');
  f.feed(new Float32Array([0.5]));
  const frozen=packet(f);
  assert.equal(frozen.user_speaking,false);
  f.api.vadSample(0.9,1000,epoch);
  assert.equal(f.api.streamPcmSpeaking(String(epoch)),'yes');
  assert.equal(f.api.windowSpeaking(),'no');
  assert.deepEqual(packet(f),frozen);
  f.api.vadSample(0,1001,epoch);
  assert.equal(f.api.streamPcmSpeaking(String(epoch)),'no');
  assert.deepEqual(packet(f),frozen);
  await f.api.stopArmed();
  assert.equal(f.api.streamPcmSpeaking(String(epoch)),'unavailable');
});

test('fresh speaking refuses stale malformed and retired capture identities',async()=>{
  const f=await armed(),epoch=Number(f.api.captureEpoch());
  assert.equal(typeof f.api.streamPcmSpeaking,'function');
  for(const raw of ['',0,null,String(epoch+1),'01','1e0','1.0','9007199254740992']) {
    assert.equal(f.api.streamPcmSpeaking(raw),'unavailable');
  }
  const boundary={capture_epoch:epoch,next_sequence:1,sample_offset:0};
  const rotation=JSON.parse(f.api.streamPcmRotate(JSON.stringify(boundary)));
  assert.equal(f.api.streamPcmSpeaking(String(epoch)),'unavailable');
  assert.equal(f.api.streamPcmSpeaking(String(rotation.capture_epoch)),'no');
  await f.api.stopArmed();
});

test('fresh speaking cannot treat a faulted input as observed silence',async()=>{
  const f=await armed(),epoch=Number(f.api.captureEpoch());
  assert.equal(typeof f.api.streamPcmSpeaking,'function');
  f.feed(new Float32Array([NaN]));
  assert.equal(f.api.streamPcmSpeaking(String(epoch)),'unavailable');
  await f.api.stopArmed();
});

test('incremental FIFO retains every ordered PCM sample until exact acknowledgment', async()=>{
  const f=await armed();
  assert.equal(f.seen.gets.length,1);
  assert.equal(f.seen.gets[0].audio.channelCount,1);
  assert.equal(f.seen.recorders.length,0);
  const epoch=Number(f.api.captureEpoch());
  assert.equal(state(f).capture_epoch,epoch);
  assert.equal(state(f).sample_rate,16000);
  assert.equal(packet(f),null);
  f.feed(new Float32Array([0,0.5,-0.5]));
  f.api.vadSample(0.9,1000,epoch);
  const first=packet(f);
  assert.equal(first.sequence,1);assert.equal(first.sample_offset,0);
  assert.equal(first.sample_count,3);assert.equal(first.capture_epoch,epoch);
  assert.equal(first.user_speaking,true);
  const bytes=Buffer.from(first.pcm16_base64,'base64');
  assert.equal(bytes.length,6);
  assert.deepEqual([0,2,4].map(i=>bytes.readInt16LE(i)),[0,16383,-16384]);
  f.feed(new Float32Array([1,-1,0.25]));
  f.api.vadSample(0,1001,epoch);
  assert.deepEqual(packet(f),first,'a pending packet and its hints stay frozen');
  assert.equal(f.api.streamPcmAck(JSON.stringify({...original(first),sample_offset:1})),'mismatch');
  assert.deepEqual(packet(f),first);
  assert.equal(acknowledge(f,first),'confirmed');
  assert.equal(acknowledge(f,first),'confirmed','an exact repeated local acknowledgment is idempotent');
  const second=packet(f);
  assert.equal(second.sequence,2);assert.equal(second.sample_offset,3);
  assert.equal(second.sample_count,3);assert.equal(second.user_speaking,false);
  assert.deepEqual([0,2,4].map(i=>Buffer.from(second.pcm16_base64,'base64').readInt16LE(i)),[32767,-32768,8191]);
  assert.equal(acknowledge(f,first),'confirmed');
  assert.deepEqual(packet(f),second,'an old exact acknowledgment cannot consume new samples');
  assert.equal(acknowledge(f,second),'confirmed');assert.equal(packet(f),null);
  await f.api.stopArmed();
  assert.equal(f.api.streamPcmRelease(String(epoch)),'released');
});

test('incremental packetization crosses callback boundaries without gaps or snapshot replacement',async()=>{
  const f=await armed();
  const samples=Float32Array.from({length:33001},(_,i)=>((i%1024)-512)/512);
  for(let i=0;i<samples.length;i+=127)f.feed(samples.subarray(i,i+127));
  const actual=[];let offset=0,sequence=1;
  for(let p=packet(f);p;p=packet(f)) {
    assert.equal(p.sequence,sequence++);assert.equal(p.sample_offset,offset);
    assert.ok(p.sample_count>0&&p.sample_count<=16000);
    const b=Buffer.from(p.pcm16_base64,'base64');assert.equal(b.length,p.sample_count*2);
    for(let i=0;i<b.length;i+=2)actual.push(b.readInt16LE(i));
    offset+=p.sample_count;assert.equal(acknowledge(f,p),'confirmed');
  }
  assert.equal(offset,samples.length);
  assert.deepEqual(actual,Array.from(samples,x=>Math.trunc(x<0?x*32768:x*32767)));
  await f.api.stopArmed();
});

test('incremental capture faults preserve the original packet and stop further acquisition',async()=>{
  for(const malformed of [new Float32Array([NaN]),new Float32Array([Infinity])]) {
    const f=await armed();f.feed(new Float32Array([0.5]));const first=packet(f);
    f.feed(malformed);
    assert.equal(state(f).fault,'invalid_pcm');
    assert.deepEqual(packet(f),first);
    assert.ok(f.seen.stopped>0);
    assert.equal(f.api.streamPcmConfigure(true),'busy');
    assert.equal(await f.api.startArmed(),'unavailable');
    assert.equal(f.seen.gets.length,1,'a retained fault cannot open a replacement microphone');
    await f.api.stopArmed();
  }
});

test('declared stream capacity stops visibly while retaining its bounded original prefix',async()=>{
  const f=await armed();
  for(let i=0;i<60;i++)f.feed(new Float32Array(16000));
  const first=packet(f), before=state(f);
  assert.equal(before.buffered_samples,960000);
  f.feed(new Float32Array([0.5]));
  assert.equal(state(f).fault,'stream_limit');
  assert.equal(state(f).buffered_samples,960000);
  assert.deepEqual(packet(f),first);
  assert.ok(JSON.parse(f.api.pollEvents()).some(e=>e.type==='stream-pcm-fault'&&e.reason==='stream_limit'));
  assert.ok(f.seen.stopped>0);
  await f.api.stopArmed();
});

test('capture retirement retains FIFO identity until the actual original context closes',async()=>{
  const closing=gate(),f=await armed({closeGate:closing});
  f.feed(new Float32Array([0.25]));const first=packet(f),epoch=first.capture_epoch;
  const stopped=f.api.stopArmed();
  assert.equal(f.api.streamPcmRelease(String(epoch)),'busy');
  assert.deepEqual(packet(f),first);
  assert.equal(f.api.streamPcmRelease(String(epoch+1)),'mismatch');
  assert.equal(f.api.streamPcmConfigure(false),'busy');
  closing.resolve();assert.equal(await stopped,'idle');
  assert.equal(f.api.streamPcmRelease(String(epoch)),'released');
  assert.equal(packet(f),null);
  assert.equal(f.api.streamPcmConfigure(false),'legacy');
});

test('incremental capture refuses incompatible modes and a non16k source without fallback',async()=>{
  const f=fixture();assert.equal(f.api.streamPcmConfigure(true),'stream-pcm');
  assert.equal(f.api.nativePcmConfigure(true),'unavailable');
  assert.equal(f.api.windowConfigure(true),'unavailable');
  assert.equal(f.seen.gets.length,0);
  const wrong=fixture({sampleRate:48000});
  assert.equal(wrong.api.streamPcmConfigure(true),'stream-pcm');
  assert.equal(await wrong.api.startArmed(),'unavailable');
  assert.equal(wrong.seen.recorders.length,0);
  assert.equal(packet(wrong),null);
  assert.ok(wrong.seen.stopped>0);
  await wrong.api.stopArmed();
});

test('playback and local acknowledgment preserve the armed capture and utterance',async()=>{
  const f=await armed(),epoch=Number(f.api.captureEpoch());
  f.feed(new Float32Array([0.5]));const first=packet(f);
  await f.api.queuePcm(Buffer.alloc(320).toString('base64'),16000);
  f.api.vadSample(0.9,1000,epoch);f.api.vadSample(0.9,1500,epoch);
  assert.equal(f.api.playbackActive(),'yes','incremental capture cannot select acoustic barge timing');
  assert.equal(await f.api.beginClip(),'busy');
  assert.equal(await f.api.startMic(),'unavailable');
  assert.equal(f.api.nextWindowUtterance(),'unavailable');
  assert.equal(Number(f.api.captureEpoch()),epoch);
  f.feed(new Float32Array([-0.5]));assert.equal(acknowledge(f,first),'confirmed');
  const second=packet(f);assert.equal(second.sample_offset,1);
  assert.equal(second.capture_epoch,epoch);assert.equal(second.user_speaking,true);
  f.api.stopAudio();assert.equal(f.seen.stopped,0);
  assert.equal(f.seen.gets.length,1);assert.equal(f.seen.recorders.length,0);
  assert.equal(state(f).active,true);
  assert.equal(JSON.parse(f.api.pollEvents()).some(e=>e.type==='barge'||e.type==='speech-end'),false);
  await f.api.stopArmed();
});

test('malformed acknowledgments cannot consume a retained original',async()=>{
  const f=await armed();f.feed(new Float32Array([0.25]));const p=packet(f);
  for(const raw of ['{','[]','{}','x'.repeat(513),JSON.stringify({...original(p),extra:1}),
    JSON.stringify({...original(p),sample_count:true}),JSON.stringify({...original(p),sequence:1.5}),
    JSON.stringify({...original(p),capture_epoch:p.capture_epoch+1})]) {
    assert.equal(f.api.streamPcmAck(raw),'mismatch');assert.deepEqual(packet(f),p);
  }
  assert.equal(acknowledge(f,p),'confirmed');assert.equal(packet(f),null);
  await f.api.stopArmed();
});

test('an original FIFO survives disposal and cannot alias a subsequent capture identity',async()=>{
  const f=await armed();f.feed(new Float32Array([0.5]));const originalPacket=packet(f);
  f.api.dispose();await f.api.stopArmed();
  assert.deepEqual(packet(f),originalPacket);
  assert.equal(f.api.streamPcmRelease(String(originalPacket.capture_epoch)),'released');
  assert.equal(await f.api.startArmed(),'armed');
  f.feed(new Float32Array([-0.5]));const next=packet(f);
  assert.ok(next.capture_epoch>originalPacket.capture_epoch);
  await f.api.stopArmed();
  assert.equal(f.api.streamPcmRelease(String(originalPacket.capture_epoch)),'mismatch');
  assert.deepEqual(packet(f),next);
  assert.equal(f.api.streamPcmRelease(String(next.capture_epoch)),'released');
});

test('one retained readiness notification wakes on original PCM or capture retirement without polling',async()=>{
  const f=await armed(),epoch=Number(f.api.captureEpoch());
  const first=f.api.streamPcmWait(String(epoch));
  assert.equal(f.api.streamPcmWait(String(epoch)),first);
  let completed=false;first.then(()=>{completed=true;});
  await Promise.resolve();assert.equal(completed,false);
  assert.equal(await f.api.streamPcmWait(String(epoch+1)),'unavailable');
  f.feed(new Float32Array([0.25]));
  const observed=JSON.parse(await first);
  assert.equal(observed.capture_epoch,epoch);assert.equal(observed.buffered_samples,1);
  assert.equal(acknowledge(f,packet(f)),'confirmed');
  const stopped=f.api.streamPcmWait(String(epoch));
  assert.notEqual(stopped,first);
  const retirement=f.api.stopArmed();
  assert.equal(JSON.parse(await stopped).active,false);
  await retirement;
  assert.equal(f.api.streamPcmRelease(String(epoch)),'released');
  assert.equal(await f.api.streamPcmWait(String(epoch)),'unavailable');
});

function boundary(f) {
  const current=state(f);
  return {capture_epoch:current.capture_epoch,next_sequence:current.next_sequence,sample_offset:current.sample_offset};
}
function rotate(f,original) { return f.api.streamPcmRotate(JSON.stringify(original)); }

test('segment rollover preserves every unacknowledged sample and the original live microphone',async()=>{
  const f=await armed();
  assert.equal(typeof f.api.streamPcmRotate,'function');
  const epoch=Number(f.api.captureEpoch());
  f.api.vadSample(0.9,1000,epoch);
  f.feed(new Float32Array([0.5,-0.5]));const first=packet(f);
  assert.equal(acknowledge(f,first),'confirmed');
  const original=boundary(f);
  // These samples arrive while the caller waits for its exact native finish.
  f.feed(new Float32Array([0.25,-0.25,1]));
  const proof=JSON.parse(rotate(f,original));
  assert.equal(proof.schema_version,'1.0');assert.equal(proof.protocol,'browser_pcm_segment_v1');
  assert.deepEqual(proof.original,original);assert.ok(proof.capture_epoch>epoch);
  assert.equal(Number(f.api.captureEpoch()),proof.capture_epoch);
  const after=state(f);
  assert.equal(after.sample_offset,0);assert.equal(after.next_sequence,1);
  assert.equal(after.received_samples,3);assert.equal(after.buffered_samples,3);
  assert.equal(after.active,true);
  const next=packet(f);
  assert.equal(next.user_speaking,true,'decoder rollover does not end the speaker');
  assert.equal(next.capture_epoch,proof.capture_epoch);
  assert.deepEqual([0,2,4].map(i=>Buffer.from(next.pcm16_base64,'base64').readInt16LE(i)),[8191,-8192,32767]);
  assert.equal(acknowledge(f,first),'mismatch','a preceding segment cannot acknowledge the new packet');
  assert.equal(rotate(f,original),JSON.stringify(proof),'exact local rollover readback is idempotent');
  assert.deepEqual(packet(f),next);
  f.feed(new Float32Array([-1]));
  assert.equal(acknowledge(f,next),'confirmed');
  assert.equal(packet(f).sample_offset,3);
  assert.equal(f.seen.gets.length,1);assert.equal(f.seen.stopped,0);assert.equal(f.seen.recorders.length,0);
  await f.api.stopArmed();
});

test('segment rollover refuses a pending packet stale boundaries and malformed originals',async()=>{
  const f=await armed();assert.equal(typeof f.api.streamPcmRotate,'function');
  f.feed(new Float32Array([0.5]));const first=packet(f),before=state(f);
  assert.equal(rotate(f,boundary(f)),'busy');assert.deepEqual(packet(f),first);
  assert.equal(acknowledge(f,first),'confirmed');const original=boundary(f);
  for(const raw of ['{','[]','{}','x'.repeat(513),
    JSON.stringify({...original,extra:true}),JSON.stringify({...original,next_sequence:2.5}),
    JSON.stringify({...original,sample_offset:0}),JSON.stringify({...original,capture_epoch:before.capture_epoch+1}),
    `{"capture_epoch":${original.capture_epoch},"next_sequence":2,"next_sequence":2,"sample_offset":1}`,
    `{"capture_epoch":${original.capture_epoch},"next_sequence":2e0,"sample_offset":1}`]) {
    assert.equal(f.api.streamPcmRotate(raw),'mismatch');
    assert.equal(state(f).sample_offset,1);
  }
  const proof=JSON.parse(rotate(f,original));assert.ok(proof.capture_epoch>original.capture_epoch);
  await f.api.stopArmed();
});

test('sequential decoder boundaries can exceed sixty seconds without releasing the armed mic',async()=>{
  const f=await armed();assert.equal(typeof f.api.streamPcmRotate,'function');
  let total=0;
  for(let segment=0;segment<4;segment++) {
    for(let block=0;block<30;block++) {
      f.feed(new Float32Array(16000).fill((block%2?1:-1)*0.25));
      for(let p=packet(f);p;p=packet(f)) {
        total+=p.sample_count;assert.equal(acknowledge(f,p),'confirmed');
      }
    }
    const original=boundary(f);
    assert.equal(original.sample_offset,480000);
    const proof=JSON.parse(rotate(f,original));assert.ok(proof.capture_epoch>original.capture_epoch);
    assert.equal(state(f).received_samples,0);assert.equal(state(f).fault,null);
  }
  assert.equal(total,1920000);assert.equal(f.seen.gets.length,1);assert.equal(f.seen.stopped,0);
  await f.api.stopArmed();
});

test('rollover wakes the old local notification and keeps the new notification owned',async()=>{
  const f=await armed();assert.equal(typeof f.api.streamPcmRotate,'function');
  const original=boundary(f),wait=f.api.streamPcmWait(String(original.capture_epoch));
  const proof=JSON.parse(rotate(f,original));
  assert.equal(JSON.parse(await wait).capture_epoch,original.capture_epoch);
  assert.equal(await f.api.streamPcmWait(String(original.capture_epoch)),'unavailable');
  const next=f.api.streamPcmWait(String(proof.capture_epoch));
  assert.equal(f.api.streamPcmWait(String(proof.capture_epoch)),next);
  f.feed(new Float32Array([0.5]));
  assert.equal(JSON.parse(await next).capture_epoch,proof.capture_epoch);
  await f.api.stopArmed();
});

test('fault or held actual cleanup cannot roll a local decoder segment into fresh capacity',async()=>{
  const closing=gate(),f=await armed({closeGate:closing});
  assert.equal(typeof f.api.streamPcmRotate,'function');
  f.feed(new Float32Array([0.5]));assert.equal(acknowledge(f,packet(f)),'confirmed');
  const original=boundary(f);f.feed(new Float32Array([NaN]));
  assert.equal(rotate(f,original),'unavailable');
  assert.equal(state(f).capture_epoch,original.capture_epoch);
  closing.resolve();await f.api.stopArmed();
  assert.equal(rotate(f,original),'unavailable');
});

test('a clean stopped microphone cannot roll while its original context cleanup is held',async()=>{
  const closing=gate(),f=await armed({closeGate:closing});
  f.feed(new Float32Array([0.5]));assert.equal(acknowledge(f,packet(f)),'confirmed');
  const original=boundary(f),stop=f.api.stopArmed();
  assert.equal(state(f).fault,null);assert.equal(state(f).cleanup_pending,true);
  assert.equal(rotate(f,original),'unavailable');assert.equal(state(f).capture_epoch,original.capture_epoch);
  closing.resolve();await stop;
  assert.equal(f.api.streamPcmRelease(String(original.capture_epoch)),'released');
});
