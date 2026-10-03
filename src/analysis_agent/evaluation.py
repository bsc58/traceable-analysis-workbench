"""Trusted offline evaluation receipts, separate from the Agent's tool registry.

Existing immutable release rows index public CAS receipts; no schema migration is
needed. The trusted operator submits measured metadata; known reference keys are
rejected as defense in depth. This is not a general-purpose secrecy classifier.
"""
from sqlalchemy import select
from .contracts import WorkbenchError, digest
from .storage import releases

KINDS = {'plan', 'trial', 'summary'}
FORBIDDEN_FIELDS = {'answers', 'expected_values', 'reference_answers', 'raw_inputs', 'skill_text', 'necessary_facts'}


def _check(value):
    if isinstance(value, dict):
        if FORBIDDEN_FIELDS.intersection(value):
            raise WorkbenchError('evaluation_payload_forbidden', 'Reference material cannot be stored as public result metadata', 422)
        for child in value.values(): _check(child)
    elif isinstance(value, list):
        for child in value: _check(child)


class EvaluationLog:
    def __init__(self, workbench, project_id):
        workbench._authorize(project_id)
        self.w, self.project = workbench, project_id
        self.namespace = 'evaluation@1:' + digest({'project': project_id, 'actor': workbench.actor})

    def append(self, kind, key, payload):
        self.w._writable(); self.w._authorize(self.project)
        if kind not in KINDS or not isinstance(key, str) or not 1 <= len(key) <= 200:
            raise WorkbenchError('invalid_evaluation_record', 'Explicit receipt kind and bounded key required', 422)
        _check(payload)
        value = {'schema_version': 'evaluation_receipt@1', 'project_id': self.project,
                 'actor': self.w.actor, 'kind': kind, 'key': key, 'payload': payload}
        value_hash = self.w.objects.put(value)
        with self.w.store.engine.begin() as connection:
            self.w.store.release(connection, self.namespace, kind + ':' + key, value_hash)
        return {'key': key, 'kind': kind, 'content_hash': value_hash}

    def read(self, kind=None):
        self.w._authorize(self.project)
        with self.w.store.engine.connect() as connection:
            refs = list(connection.execute(select(releases.c.content_hash).where(
                releases.c.namespace == self.namespace).order_by(releases.c.ref)).scalars())
        result = []
        for ref in refs:
            receipt = self.w.objects.get(ref)
            if receipt['actor'] != self.w.actor or receipt['project_id'] != self.project:
                raise WorkbenchError('evaluation_scope_mismatch', 'Receipt scope does not match its namespace', 409)
            if kind is None or receipt['kind'] == kind:
                result.append({**receipt, 'content_hash': ref})
        return result
