"""Official OpenAI-compatible providers; credentials never enter persisted payloads."""
from dataclasses import dataclass
from typing import Callable, Protocol
import json
import os

import httpx

from .contracts import WorkbenchError, canonical


@dataclass(frozen=True)
class ProviderCapabilities:
    tool_calls: str = 'supported'
    json_output: str = 'supported'
    strict_json_schema: str = 'unsupported'
    idempotency_key: str = 'unsupported'
    lookup_by_id: str = 'unsupported'


class ProviderFailure(WorkbenchError):
    def __init__(self, code):
        super().__init__(code, 'Provider request did not produce a reliably saved response', 503)


class Provider(Protocol):
    name: str
    fixture: bool
    capabilities: ProviderCapabilities
    def check_ready(self): ...
    def complete(self, payload: dict) -> dict: ...


class CompatibleProvider:
    fixture = False
    capabilities = ProviderCapabilities()
    SETTINGS = {
        'deepseek': ('DEEPSEEK_API_KEY', 'https://api.deepseek.com/chat/completions'),
        'xai': ('XAI_API_KEY', 'https://api.x.ai/v1/chat/completions'),
    }

    def __init__(self, name: str, *, transport=None):
        if name not in self.SETTINGS:
            raise WorkbenchError('unsupported_provider', 'Only registered official providers are available')
        self.name = name
        self.transport = transport  # Trusted test injection, not task configuration.

    def check_ready(self):
        if not os.environ.get(self.SETTINGS[self.name][0]):
            raise WorkbenchError('provider_key_missing', 'Required provider environment variable is not set', 503)

    def complete(self, payload):
        self.check_ready()
        body = canonical(payload)
        if len(body) > 200_000:
            raise WorkbenchError('request_too_large', 'Provider body exceeds 200KB', 422)
        env_name, endpoint = self.SETTINGS[self.name]
        # No automatic retries, redirects, environment proxy, or request logging.
        try:
            with httpx.Client(timeout=60, follow_redirects=False, trust_env=False, transport=self.transport) as client:
                response = client.post(endpoint, content=body,
                    headers={'Authorization': 'Bearer ' + os.environ[env_name], 'Content-Type': 'application/json'})
                if response.status_code == 402:
                    raise ProviderFailure('insufficient_balance')
                if response.status_code >= 400:
                    try:
                        error = response.json().get('error', {})
                        code = str(error.get('code', '')).lower()
                        message = str(error.get('message', '')).lower()
                    except (ValueError, AttributeError):
                        code = message = ''
                    if any(term in code + ' ' + message for term in ('insufficient_balance', 'insufficient balance', 'insufficient_quota', 'credits exhausted', '余额不足')):
                        raise ProviderFailure('insufficient_balance')
                    raise ProviderFailure('provider_http_error')
                raw = response.json()
                if not isinstance(raw, dict):
                    raise ProviderFailure('provider_invalid_response')
                return {'body': raw, 'request_id': response.headers.get('x-request-id') or response.headers.get('request-id') or raw.get('id'),
                        'provider': self.name, 'fixture': False}
        except ProviderFailure:
            raise
        except Exception:
            # Never serialize httpx requests/exceptions (they can contain headers).
            raise ProviderFailure('provider_transport_unknown') from None


class DeepSeekProvider(CompatibleProvider):
    def __init__(self, **kwargs): super().__init__('deepseek', **kwargs)


class GrokProvider(CompatibleProvider):
    def __init__(self, **kwargs): super().__init__('xai', **kwargs)


class FixtureProvider:
    """A deterministic test double, never a model-quality observation."""
    name = 'fixture'
    fixture = True
    capabilities = ProviderCapabilities()

    def __init__(self, responder: Callable[[dict], dict]):
        self.responder = responder
        self.calls = 0

    def check_ready(self): pass

    def complete(self, payload):
        self.calls += 1
        action = self.responder(payload)
        return {'provider': self.name, 'fixture': True, 'request_id': f'fixture-{self.calls}',
                'body': {'id': f'fixture-{self.calls}', 'model': payload['model'],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 10},
                    'choices': [{'message': {'role': 'assistant', 'content': json.dumps(action)}}]}}
