"""Finite synthetic fixture checks: no real microphone, native engine or download."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))
spec = importlib.util.spec_from_file_location('dictation_fixture', ROOT / 'docs/fixtures/dictation/serve.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class FixtureTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'posix', 'Private helper state requires POSIX')
    def test_real_helper_and_proxy_with_synthetic_runtime(self):
        from dictation_proxy import DictationProxy
        with tempfile.TemporaryDirectory() as home:
            helper, thread, runtime = fixture.start_synthetic(home)
            try:
                helper.service.loading_thread.join(2)
                proxy = DictationProxy(home, helper.server_port, enabled=True)
                status, value = proxy.request('status')
                self.assertEqual((status, value['state']), (200, 'ready'))
                _, start = proxy.request('start', {'intervalSeconds': .25})
                session = start['sessionId']
                for sequence in range(30):
                    code, value = proxy.request('audio', b'\0' * 6400, session=session, sequence=sequence)
                    self.assertEqual((code, value['acceptedSequence']), (200, sequence))
                _, observed = proxy.request('events', {'sessionId': session, 'after': 0})
                events = observed['events']
                self.assertTrue(any(event['complete'] for event in events))
                self.assertTrue(any(not event['complete'] for event in events))
                self.assertTrue(all(event['epoch'] == 1 for event in events))
                self.assertEqual(runtime.audio_packets, 30)
                last = events[-1]['id']
                _, reset = proxy.request('reset', {'sessionId': session})
                self.assertEqual(reset['epoch'], 2)
                for sequence in range(30, 33):
                    proxy.request('audio', b'\0' * 6400, session=session, sequence=sequence)
                _, after = proxy.request('events', {'sessionId': session, 'after': last})
                self.assertTrue(after['events'])
                self.assertTrue(all(event['epoch'] == 2 for event in after['events']))
                self.assertNotIn('Retired fixture text', json.dumps(after))
                _, ended = proxy.request('cancel', {'sessionId': session})
                self.assertTrue(ended['ended'])
                self.assertEqual(runtime.resets, 1)
                self.assertFalse(runtime.active)
                self.assertFalse(hasattr(runtime, 'calls'), 'runtime must not retain PCM payloads')
                _, disabled = proxy.request('status', demo=True)
                self.assertEqual(disabled['state'], 'disabled')
            finally:
                helper.shutdown(); helper.server_close(); thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertTrue(runtime.closed)

    @unittest.skipUnless(shutil.which('node'), 'Node is optional for bootstrap checks')
    def test_generated_capture_is_inert_until_gesture_and_releases_resources(self):
        check = r"""
const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),code=fs.readFileSync(0,'utf8');
for(const query of ['?demo','?x=1&demo&y=2']){
 const ctx={location:{search:query},localStorage:{setItem(){throw Error('demo storage')}},navigator:new Proxy({},{get(){throw Error('demo capture')}})};
 vm.runInNewContext(code,ctx);
}
const contexts=[],connections=[],events={},docEvents={},labels=[];
let originalCalls=0,storage=0,stops=0;
function node(){return {frequency:{value:0},gain:{value:0},connect(to){connections.push(to)},disconnect(){},start(){this.started=true},stop(){this.stopped=true}}}
class Context{
 constructor(){this.state='suspended';this.destination={speaker:true};contexts.push(this)}
 createMediaStreamDestination(){const destination=node(),track={stop(){stops++},addEventListener(){}};destination.stream={getTracks:()=>[track],addEventListener(){}};return destination}
 createOscillator(){this.oscillator=node();return this.oscillator}createGain(){return node()}
 async resume(){this.state='running'}async close(){this.state='closed'}
}
const document={hidden:false,body:{appendChild(value){labels.push(value)}},createElement(){return {style:{},setAttribute(){}}},addEventListener(type,fn){docEvents[type]=fn}};
const navigator={mediaDevices:{getUserMedia(){originalCalls++;throw Error('real capture forbidden')}}};
vm.runInNewContext(code,{location:{search:''},localStorage:{setItem(){storage++}},document,navigator,AudioContext:Context,DOMException,addEventListener:(type,fn)=>events[type]=fn});
(async()=>{
 assert.equal(storage,1);assert.equal(contexts.length,0,'bootstrap must not start audio');
 assert.equal(labels[0].textContent,'Simulated audio · microphone not used');
 const first=await navigator.mediaDevices.getUserMedia({audio:true,video:false});assert.equal(contexts.length,1);assert(contexts[0].oscillator.started);assert(!connections.some(n=>n.speaker));first.getTracks()[0].stop();assert.equal(contexts[0].state,'closed');assert(contexts[0].oscillator.stopped);
 await navigator.mediaDevices.getUserMedia({audio:true});document.hidden=true;docEvents.visibilitychange();assert.equal(contexts[1].state,'closed');document.hidden=false;
 await navigator.mediaDevices.getUserMedia({audio:true});events.pagehide();assert.equal(contexts[2].state,'closed');
 assert.equal(originalCalls,0);assert.equal(stops,3);console.log('generated capture lifecycle passed');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
        result = subprocess.run(['node', '-e', check], input=fixture.review_bootstrap(True).decode(),
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_native_mode_has_no_capture_substitution(self):
        script = fixture.review_bootstrap(False).decode()
        self.assertIn('localStorage.setItem', script)
        self.assertNotIn('AudioContext', script)
        self.assertNotIn('getUserMedia', script)
        self.assertLess(script.index('if(/[?&]demo'), script.index('localStorage.setItem'))

    def test_modes_are_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as home:
            result = subprocess.run([sys.executable, str(ROOT / 'docs/fixtures/dictation/serve.py'),
                                     '--home', home, '--port', '0', '--synthetic', '--helper-port', '18879'],
                                    text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertIn('not allowed with argument', result.stderr)


if __name__ == '__main__':
    unittest.main()
