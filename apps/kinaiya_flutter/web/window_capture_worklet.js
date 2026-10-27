/* Trusted microphone PCM collector. No inference, networking or retention. */
class KinaiyaWindowPCM extends AudioWorkletProcessor {
  process(inputs) {
    const channel=inputs[0] && inputs[0][0];
    if(channel && channel.length)this.port.postMessage(new Float32Array(channel));
    return true;
  }
}
registerProcessor('kinaiya-window-pcm',KinaiyaWindowPCM);
