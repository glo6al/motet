"""Finite synthetic fixture checks: no real microphone, native engine or download."""
import importlib.util
import http.client
import json
import os
from pathlib import Path
import shutil
import queue
import re
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))
spec = importlib.util.spec_from_file_location('dictation_fixture', ROOT / 'docs/fixtures/dictation/serve.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class FixtureTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is optional for diagnostic checks')
    def test_motion_probe_is_opt_in_hidden_bounded_and_observes_real_animation_objects(self):
        self.assertNotIn(b'dictationMotionProbe', fixture.review_bootstrap(False))
        check = r"""
const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),code=fs.readFileSync(0,'utf8');
function harness(pathname,search){
 let now=0,next=0,calls=0;const nodes=[],frames=new Map(),animations=[];
 class Element{
  constructor(id){this.id=id;this.height=100;this.opacity=1}
  getBoundingClientRect(){return {height:this.height}}
  animate(keyframes,options){calls++;const events={},animation={effect:{getTiming:()=>options},currentTime:0,playState:'running',addEventListener:(type,fn)=>events[type]=fn,events};animations.push(animation);return animation}
 }
 const original=Element.prototype.animate;
 const document={hidden:false,body:{appendChild(node){nodes.push(node)}},createElement(){return {style:{},dataset:{},setAttribute(k,v){this[k]=v}}}};
 const context={location:{pathname,search},document,Element,URLSearchParams,performance:{now:()=>now},getComputedStyle:node=>({opacity:String(node.opacity)}),requestAnimationFrame:fn=>{frames.set(++next,fn);return next},cancelAnimationFrame:id=>frames.delete(id),localStorage:{setItem(){if(/[?&]demo\b/.test(search))throw Error('demo storage')}}};
 vm.runInNewContext(code,context);
 return {nodes,frames,animations,Element,original,calls:()=>calls,frame(){now+=16;const pending=[...frames.values()];frames.clear();for(const fn of pending)fn()}};
}
for(const [path,query]of [['/review',''],['/review','?motionProbe=0'],['/other','?motionProbe=1'],['/review','?demo&motionProbe=1']]){
 const h=harness(path,query);assert.equal(h.nodes.length,0);assert.equal(h.Element.prototype.animate,h.original);assert.equal(h.calls(),0);
}
const h=harness('/review','?motionProbe=1'),output=h.nodes[0];assert(output.hidden);assert.equal(output['aria-hidden'],'true');assert.equal(output.style.display,'none');assert.equal(h.calls(),0,'diagnostics must not start animations');
const target=new h.Element('officer'),options={duration:200,easing:'cubic-bezier(.22,.7,.2,1)'};
const first=target.animate([{height:'100px'},{height:'300px'}],options);target.height=150;target.opacity=.5;first.currentTime=80;h.frame();target.height=300;first.playState='finished';first.events.finish();
let records=JSON.parse(output.textContent).records;assert.equal(records.length,1);assert.equal(records[0].duration,200);assert.equal(records[0].easing,options.easing);assert.equal(records[0].target,'officer');assert.equal(records[0].state,'finished');assert.deepEqual(records[0].samples.map(s=>s.height),[100,150,300]);assert.equal(records[0].samples[1].opacity,.5);assert.equal(records[0].samples[1].currentTime,80);assert.equal(h.frames.size,1);assert.deepEqual(records[0].keyframes,[{height:'100px'},{height:'300px'}]);target.height=143;h.frame();records=JSON.parse(output.textContent).records;assert.equal(records[0].settled.height,143);assert.equal(output.dataset.lastSettledHeight,'143');assert.equal(h.frames.size,0);
const second=target.animate([],options);h.frame();second.events.cancel();records=JSON.parse(output.textContent).records;assert.equal(records.at(-1).state,'cancelled');h.frame();assert.equal(h.frames.size,0);
const prompts=new h.Element('workspaceOfficerPrompts'),promptAnimation=prompts.animate([],options);prompts.height=0;prompts.opacity=0;h.frame();promptAnimation.events.finish();h.frame();records=JSON.parse(output.textContent).records;assert.equal(records.at(-1).target,'workspaceOfficerPrompts');assert.equal(records.at(-1).samples.at(-1).height,0);assert.equal(records.at(-1).samples.at(-1).opacity,0);
for(let i=0;i<14;i++){const a=target.animate([{height:'200px',opacity:0,privateText:'discard me'},{height:'unsafe text',opacity:Infinity}],options);for(let f=0;f<30;f++)h.frame();a.events.finish();h.frame()}
records=JSON.parse(output.textContent).records;assert.equal(records.length,12);assert(records.every(r=>r.samples.length<=24));assert(records.every(r=>r.keyframes.every(f=>!('privateText'in f))));assert.deepEqual(records.at(-1).keyframes,[{height:'200px',opacity:0},{}]);assert.equal(output.dataset.count,'12');assert.equal(h.frames.size,0);
const count=records.length;new h.Element('unrelated').animate([],options);assert.equal(JSON.parse(output.textContent).records.length,count);assert.equal(h.calls(),18);
console.log('motion diagnostic gate, geometry, lifecycle and bounds passed');
"""
        result = subprocess.run(['node', '-e', check], input=fixture.review_bootstrap(False, True).decode('utf-8'),
                                text=True, encoding='utf-8', capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_scripted_recognition_revises_an_earlier_word_on_the_same_line(self):
        runtime = fixture.FakeRuntime()
        runtime.call('start', interval=.25)
        events = []
        for _ in range(21):
            events.extend(runtime.call('audio')['events'])
        first = next(event for event in events if event['text'] == 'Which agent')
        revised = next(event for event in events if event['text'] == 'Which colleague changed the pricing')
        final = events[-1]
        self.assertEqual((first['lineId'], first['epoch']), (revised['lineId'], revised['epoch']))
        self.assertEqual((final['lineId'], final['epoch']), (first['lineId'], first['epoch']))
        self.assertFalse(first['complete'])
        self.assertFalse(revised['complete'])
        self.assertTrue(final['complete'])
        self.assertEqual(final['text'], 'Which colleague changed the pricing file today?')
        self.assertEqual(runtime.audio_packets, 21)
        self.assertFalse(hasattr(runtime, 'calls'), 'the fixture must not retain PCM')

    def test_fictional_sse_covers_every_requested_desk(self):
        events = fixture.fixture_events(1234)
        agents = {event['agent']: event for event in events if event['kind'] == 'agent'}
        self.assertEqual(set(agents), {row[0] for row in fixture.DESKS.values()})
        self.assertEqual([e['seq'] for e in events], list(range(1, len(events) + 1)))
        self.assertTrue(agents[fixture.DESKS['subagent'][0]]['sub'])
        self.assertEqual(agents[fixture.DESKS['subagent'][0]]['parent'], fixture.DESKS['claude'][0])
        self.assertTrue(any(e['kind'] == 'turn_end' and e['agent'] == fixture.DESKS['ended'][0] for e in events))
        self.assertTrue(all(e['boot'] == 'dictation-fixture' for e in events))

    @unittest.skipUnless(os.name == 'posix', 'Private helper state requires POSIX')
    def test_review_http_variants_keep_all_product_actions_refused(self):
        with tempfile.TemporaryDirectory() as home:
            process = subprocess.Popen([sys.executable, '-u', str(ROOT / 'docs/fixtures/dictation/serve.py'),
                                        '--home', home, '--port', '0', '--synthetic'],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
                                       env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
            lines = queue.Queue()
            reader_errors = []
            def read_lines():
                try:
                    for line in process.stdout:
                        lines.put(line)
                except (UnicodeError, OSError) as error:
                    reader_errors.append(error)
                    lines.put(error)
            reader = threading.Thread(target=read_lines, daemon=True)
            reader.start()
            try:
                first = lines.get(timeout=10)
                self.assertIsInstance(first, str, 'Fixture output could not be decoded as UTF-8')
                port = int(re.search(r'127\.0\.0\.1:(\d+)', first).group(1))
                def request(path, method='GET', body=None):
                    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
                    try:
                        connection.request(method, path, body, {'X-Live-Room-Key': fixture.OWNER})
                        response = connection.getresponse()
                        return response.status, response.read()
                    finally:
                        connection.close()
                for desk in (*fixture.DESKS, 'stage', 'motet'):
                    code, page = request('/review?desk=' + desk)
                    self.assertEqual(code, 200, desk)
                    self.assertIn(('const desk=' + json.dumps(desk)).encode(), page)
                    self.assertIn(b'Simulated audio', page)
                code, page = request('/review?desk=claude&attachments=a03-five')
                self.assertEqual(code, 200)
                self.assertNotIn(b"output.id='dictationMotionProbe'", page)
                self.assertIn(b'createAttachmentsFixture({enabled:true', page)
                self.assertIn(b"globalThis.__dictationReviewFixture===true&&location.pathname==='/review'", page)
                self.assertIn(b"command.type==='send_sample'", page)
                if shutil.which('node'):
                    script = re.search(rb'<script nonce="[^"]+">(.*?)</script>', page, re.S).group(1).decode('utf-8')
                    checked = subprocess.run(['node', '--check'], input=script, text=True, encoding='utf-8',
                                             capture_output=True, timeout=10)
                    self.assertEqual(checked.returncode, 0, checked.stderr)
                code, motion = request('/review?motionProbe=1')
                self.assertEqual(code, 200)
                self.assertIn(b"output.id='dictationMotionProbe'", motion)
                self.assertNotIn(b"output.id='dictationMotionProbe'", request('/review?motionProbe=0')[1])
                for path in ('/review?desk=unknown', '/review?attachments=unknown', '/review?desk=claude&desk=codex'):
                    self.assertEqual(request(path)[0], 400)
                demo = request('/?demo')[1]
                self.assertNotIn(b'__dictationReviewFixture', demo)
                self.assertNotIn(b'createAttachmentsFixture({enabled:true', demo)
                for path in ('/ask', '/agent-message', '/agent-control', '/fixture-refused-send',
                             '/officer/model-selection', '/agent-open', '/fs/fictional', '/unknown-action'):
                    self.assertEqual(request(path, 'POST', b'fictional unsent text')[0], 409, path)
                for path in ('/fs/fictional', '/unknown', '/agent-capabilities?agent=unknown'):
                    self.assertIn(request(path)[0], (404, 503))
                cap = json.loads(request('/agent-capabilities?agent=x%3Afixture-unavailable')[1])
                self.assertFalse(cap['actions']['queue']['available'])
                cap = json.loads(request('/agent-capabilities?agent=c%3Ademo-ada')[1])
                self.assertTrue(cap['actions']['queue']['available'])
                counts = json.loads(request('/fixture-status')[1])
                self.assertEqual(counts['agentMessages'], 0)
                self.assertEqual(counts['takesStarted'], 0, 'rendering review pages must not start capture')
                self.assertEqual(counts['blockedSends'], 4)
                self.assertNotIn(b'fictional unsent text', request('/fixture-status')[1])
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=5)
                reader.join(2)
                process.stdout.close(); process.stderr.close()
                self.assertFalse(reader.is_alive(), 'Fixture stdout reader did not finish')
                self.assertEqual(reader_errors, [], 'Fixture stdout reader failed')

    def test_attachment_gate_rewrite_is_exact_and_not_a_product_flag(self):
        body = (ROOT / 'src/web/index.html').read_bytes()
        rendered = fixture.review_page(body, True, 'claude', 'a03-one')
        self.assertEqual(rendered.count(b'const fixtureReview='), 1)
        self.assertEqual(rendered.count(b'globalThis.__dictationReviewFixture=true'), 1)
        self.assertNotIn(b'__dictationReviewFixture', body)
        broken = body.replace(b"if(!WORKSPACE||!DEMO||!query.has('demo')", b'if(false')
        with self.assertRaises(RuntimeError):
            fixture.review_page(broken, True, 'claude', 'a03-one')

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
                                text=True, encoding='utf-8', capture_output=True, timeout=10)
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
                                    text=True, encoding='utf-8', capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertIn('not allowed with argument', result.stderr)


if __name__ == '__main__':
    unittest.main()
