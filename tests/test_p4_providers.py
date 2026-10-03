import json
import httpx
import pytest
from analysis_agent.contracts import WorkbenchError
from analysis_agent.providers import DeepSeekProvider, GrokProvider, ProviderFailure


@pytest.mark.parametrize('provider,env,host',[(DeepSeekProvider,'DEEPSEEK_API_KEY','api.deepseek.com'),(GrokProvider,'XAI_API_KEY','api.x.ai')])
def test_official_endpoint_actual_metadata_and_no_retry(monkeypatch,provider,env,host):
    monkeypatch.setenv(env,'synthetic-test-value');calls=[]
    def handle(request):
        calls.append(request)
        assert request.url.host==host and request.headers['authorization']=='Bearer synthetic-test-value'
        return httpx.Response(200,headers={'x-request-id':'request-fixture'},json={'model':'actual-model','id':'body-id','usage':{'prompt_tokens':3,'completion_tokens':2},'choices':[]})
    p=provider(transport=httpx.MockTransport(handle));r=p.complete({'model':'configured-model','messages':[]})
    assert len(calls)==1 and r['body']['model']=='actual-model' and r['request_id']=='request-fixture'
    assert 'synthetic-test-value' not in json.dumps(r)
    assert p.capabilities.idempotency_key==p.capabilities.lookup_by_id=='unsupported'


@pytest.mark.parametrize('provider,env',[(DeepSeekProvider,'DEEPSEEK_API_KEY'),(GrokProvider,'XAI_API_KEY')])
def test_missing_key_before_http(monkeypatch,provider,env):
    monkeypatch.delenv(env,raising=False)
    p=provider(transport=httpx.MockTransport(lambda _:pytest.fail('unexpected HTTP')))
    with pytest.raises(WorkbenchError,match='environment variable'):p.check_ready()


@pytest.mark.parametrize('status',[402,429,500])
def test_failures_never_retry_or_expose_error_body(monkeypatch,status):
    monkeypatch.setenv('DEEPSEEK_API_KEY','synthetic-test-value');calls=[]
    def handle(request):
        calls.append(request);return httpx.Response(status,json={'error':{'message':'synthetic-test-value'}})
    p=DeepSeekProvider(transport=httpx.MockTransport(handle))
    with pytest.raises(ProviderFailure) as error:p.complete({'messages':[]})
    assert len(calls)==1 and 'synthetic-test-value' not in str(error.value)
    assert error.value.code==('insufficient_balance' if status==402 else 'provider_http_error')


def test_body_limit_before_http(monkeypatch):
    monkeypatch.setenv('XAI_API_KEY','synthetic-test-value')
    p=GrokProvider(transport=httpx.MockTransport(lambda _:pytest.fail('unexpected HTTP')))
    with pytest.raises(WorkbenchError) as error:p.complete({'text':'x'*200001})
    assert error.value.code=='request_too_large'
