import hashlib
import os
import tempfile
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from ai_gateway import router

class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{'ALL_PROXY':'','HTTPS_PROXY':'','HTTP_PROXY':'','all_proxy':'','https_proxy':'','http_proxy':'','A2Z_AI_DATA_DIR':self.tmp.name,'A2Z_AI_VISITOR_TOKENS':'{"alice":"'+hashlib.sha256(b'alice-token').hexdigest()+'","bob":"'+hashlib.sha256(b'bob-token').hexdigest()+'"}', 'FAL_KEY':'test-secret','A2Z_AI_COVER_RESERVE_CENTS':'10','A2Z_AI_DAILY_CAP_CENTS':'20','A2Z_AI_USER_DAILY_CAP_CENTS':'10'})
        self.env.start()
        app=FastAPI();app.include_router(router);self.c=TestClient(app)
        self.headers={'Authorization':'Bearer alice-token'}
    def tearDown(self):
        self.env.stop();self.tmp.cleanup()
    def body(self,nonce='a'*16):
        return {'model':'cover','prompt':'test cover','nonce':nonce,'accepted_reserve_cents':10}
    def test_auth_and_disabled_storage(self):
        self.assertEqual(self.c.get('/api/ai/capabilities').status_code,401)
        with patch.dict(os.environ,{'A2Z_AI_DATA_DIR':''}):
            self.assertEqual(self.c.get('/api/ai/capabilities',headers=self.headers).status_code,503)
    def test_uncertain_is_reserved_idempotent_and_private(self):
        with patch('ai_gateway.httpx.AsyncClient.post',side_effect=TimeoutError()) as post:
            a=self.c.post('/api/ai/jobs',headers=self.headers,json=self.body())
            self.assertEqual(a.status_code,200);self.assertEqual(a.json()['status'],'uncertain')
            again=self.c.post('/api/ai/jobs',headers=self.headers,json=self.body())
            self.assertEqual(a.json()['id'],again.json()['id']);self.assertEqual(post.call_count,1)
            self.assertEqual(self.c.post('/api/ai/jobs',headers=self.headers,json=self.body('b'*16)).status_code,429)
        self.assertEqual(self.c.get('/api/ai/jobs/'+a.json()['id'],headers={'Authorization':'Bearer bob-token'}).status_code,404)
        self.assertNotIn('test-secret',a.text)
    def test_quote_change_and_nonce_conflict(self):
        b=self.body();b['accepted_reserve_cents']=1
        self.assertEqual(self.c.post('/api/ai/jobs',headers=self.headers,json=b).status_code,409)
        with patch('ai_gateway.httpx.AsyncClient.post',side_effect=TimeoutError()):
            self.c.post('/api/ai/jobs',headers=self.headers,json=self.body())
        b=self.body();b['prompt']='different'
        self.assertEqual(self.c.post('/api/ai/jobs',headers=self.headers,json=b).status_code,409)
    def test_queued_recovery_never_resubmits(self):
        import httpx
        with patch('ai_gateway.httpx.AsyncClient.post',return_value=httpx.Response(200,json={'request_id':'r1'})):
            j=self.c.post('/api/ai/jobs',headers=self.headers,json=self.body()).json()
        with patch('ai_gateway.httpx.AsyncClient.get',side_effect=[httpx.Response(200,json={'status':'COMPLETED'}),httpx.Response(200,json={'images':[{'url':'https://v3.fal.media/files/test.jpg'}]})]):
            r=self.c.get('/api/ai/jobs/'+j['id'],headers=self.headers)
        self.assertEqual(r.json()['status'],'succeeded')
        self.assertEqual(len(self.c.get('/api/ai/jobs',headers=self.headers).json()),1)
if __name__=='__main__':unittest.main()
