"""Host hooks and real loopback HTTP framing; never starts WATCH or contacts providers."""
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import server
from attachments_store import AttachmentStore
from attachments_service import AttachmentService
from test_attachments_http import Resolver, KEY, SELECTION
from test_attachments_foundation import EPOCH, fence, metadata
from attachment_host import POSIX, POSIX_WHY, STORE, STORE_WHY, state_root


def live_room(home):
    """A private ~/.live-room for a test home: mode 0700 on POSIX, a protected list owned by this user on Windows."""
    if os.name == 'nt':
        import private_storage
        private_storage.make_private_folder(str(home / '.live-room'))
    else:
        (home / '.live-room').mkdir(mode=0o700)


@unittest.skipUnless(STORE, STORE_WHY)
class IntegrationTests(unittest.TestCase):
    def test_startup_disabled_and_demo_construct_nothing(self):
        for enabled,demo in ((False,'0'),(False,'1'),(True,'1')):
            with mock.patch.object(server,'ATTACHMENTS_ENABLED',enabled), mock.patch.dict(os.environ,{'WATCH_DEMO':demo}), \
                 mock.patch('attachments_store.AttachmentStore',side_effect=AssertionError('store constructed')), \
                 mock.patch('attachments_service.AttachmentService',side_effect=AssertionError('service constructed')), \
                 mock.patch('attachments_service.UnqualifiedResolver',side_effect=AssertionError('resolver constructed')):
                self.assertIsNone(server.start_attachments())
        self.assertIsNone(server.ATTACHMENTS)

    def test_enabled_test_startup_runtime_is_unqualified(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(os.path.realpath(temporary));live_room(root)
            with mock.patch.object(server,'ATTACHMENTS_ENABLED',True),mock.patch.object(server,'HOME',str(root)),mock.patch.object(server,'KEY',KEY),mock.patch.dict(os.environ,{'WATCH_DEMO':'0'}):
                service=server.start_attachments()
                try:self.assertFalse(service.capabilities('fictional','room',None)['available'])
                finally:service.close()

    def test_real_http_binary_end_and_completed_retry_no_body_wait(self):
        with tempfile.TemporaryDirectory() as temporary:
            store=AttachmentStore(state_root(temporary),enabled=True,owner_epoch=EPOCH)
            service=AttachmentService(store,resolver=Resolver())
            host=server.Server(('127.0.0.1',0),server.H);port=host.server_address[1]
            thread=threading.Thread(target=host.serve_forever,daemon=True)
            with mock.patch.object(server,'ATTACHMENTS',service),mock.patch.object(server,'KEY',KEY),mock.patch.object(server,'PORT',port),mock.patch.object(server.H,'log_message'):
                thread.start()
                def request(method,path,body=None,extra=None):
                    connection=http.client.HTTPConnection('127.0.0.1',port,timeout=2)
                    headers={'X-Live-Room-Key':KEY,**(extra or {})}
                    connection.request(method,'/api/attachments'+path,body,headers)
                    response=connection.getresponse();status=response.status;data=json.loads(response.read());connection.close()
                    return status,data
                try:
                    status,d=request('POST','/drafts',json.dumps(SELECTION),{'Content-Type':'application/json','Idempotency-Key':'fictional-integration-draft'})
                    self.assertEqual(status,201)
                    cap={'X-Attachment-Draft-Capability':d['capability']}
                    status,u=request('POST','/drafts/'+d['id']+'/upload-intents',json.dumps(dict(fence(d),**metadata(b'orbits'))),dict(cap,**{'Content-Type':'application/json','Idempotency-Key':'fictional-integration-upload'}))
                    self.assertEqual(status,201)
                    headers=dict(cap,**{'Content-Type':'application/octet-stream','Content-Length':'6','X-Attachment-Writer-Nonce':d['writerNonce'],'X-Attachment-Writer-Generation':str(d['writerGeneration']),'X-Attachment-Draft-Revision':str(u['draftRevision']),'X-Attachment-Upload-Fingerprint':u['fingerprint']})
                    route='/drafts/'+d['id']+'/uploads/'+u['id']
                    status,ready=request('PUT',route,b'orbits',headers)
                    self.assertEqual(status,200);self.assertEqual(ready['state'],'ready')
                    # The complete retry advertises the original size but transmits
                    # no body. Its retained result must not await those bytes.
                    connection=http.client.HTTPConnection('127.0.0.1',port,timeout=2)
                    connection.putrequest('PUT','/api/attachments'+route)
                    for name,value in dict(headers,**{'X-Live-Room-Key':KEY}).items():connection.putheader(name,value)
                    connection.endheaders();response=connection.getresponse()
                    self.assertEqual(response.status,200);self.assertEqual(json.loads(response.read())['state'],'ready');connection.close()
                    for method in ('GET','POST','PATCH','DELETE','PUT','OPTIONS'):
                        with mock.patch.object(server,'ATTACHMENTS',None):
                            status,data=request(method,'/storage')
                            self.assertEqual(status,503);self.assertEqual(data['error']['code'],'attachments_disabled')
                    self.assertEqual(store.storage()['usedBytes'],6)
                finally:
                    host.shutdown();host.server_close();thread.join();service.close()

if __name__=='__main__':unittest.main()
