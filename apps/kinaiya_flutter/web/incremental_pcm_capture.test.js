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
