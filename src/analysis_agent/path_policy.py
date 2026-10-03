"""Application path boundary, not an OS sandbox or a restriction on the local owner."""
from pathlib import Path
from .contracts import WorkbenchError

EVALUATION_PRIVATE = Path(__file__).resolve().parents[4] / 'work' / 'eval_private'


def runtime_path(value):
    path=Path(value).expanduser().resolve()
    protected=EVALUATION_PRIVATE.resolve()
    if path==protected or protected in path.parents:
        raise WorkbenchError('evaluation_path_forbidden','Runtime cannot access private evaluation material',403)
    return path
