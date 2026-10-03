from fastapi.testclient import TestClient

from analysis_agent.api import create_app
from analysis_agent.presentation import render_record


class NoDB:
    def history(self, project):
        return [{"id": "run_test", "state": "SUCCEEDED"}]


def test_api_requires_credential_on_data_and_health():
    client = TestClient(create_app(NoDB(), "x" * 40))
    assert client.get("/projects/demo/runs").status_code == 401
    assert client.get("/health").status_code == 401
    assert client.get("/projects/demo/runs", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/projects/demo/runs", headers={"Authorization": "Bearer " + "x" * 40}).status_code == 200


def test_report_html_escapes_injected_content_without_remote_resources():
    attack = '<script>fetch("https://example.invalid/secret")</script><img src="https://example.invalid/pixel">'
    record = {"run": {"state": "RUNNING", "id": "x", "manifest": {"question": attack}},
              "report": None, "steps": [], "events": [], "skill": {"instructions": attack},
              "catalog": {}, "tools": {}}
    result = render_record(record)
    assert "<script>" not in result
    assert '<img src=' not in result
    assert "&lt;script&gt;" in result
    assert "default-src 'none'" in result
    assert "LIVE" in result
