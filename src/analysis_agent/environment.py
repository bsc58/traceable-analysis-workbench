"""Auditable source files and installed versions of actually imported packages."""
import hashlib
import importlib.metadata as metadata
import inspect
import platform
import sys
from pathlib import Path


def loaded_packages():
    roots = {name.partition('.')[0] for name in tuple(sys.modules)}
    mapping = metadata.packages_distributions()
    names = {dist for root in roots for dist in mapping.get(root, [])}
    return {name: metadata.version(name) for name in sorted(names)}


def capture(adapter, entrypoints=(), *, package_names=None):
    package = Path(__file__).resolve().parent
    files = {'workbench/' + str(p.relative_to(package)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(package.rglob('*.py')) if '__pycache__' not in p.parts}
    module = inspect.getmodule(type(adapter))
    if module and getattr(module, '__file__', None):
        files['registered_adapter'] = hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
    paths = [package / 'cli.py', *map(Path, entrypoints)]
    for index, path in enumerate(paths):
        files[f'entrypoint/{index}/{path.name}'] = hashlib.sha256(path.read_bytes()).hexdigest()
    packages = loaded_packages() if package_names is None else {name: metadata.version(name) for name in sorted(package_names)}
    return {'schema_version':'execution_environment@1','files':files,'python':platform.python_version(),
            'implementation':platform.python_implementation(),'platform':platform.platform(),
            'loaded_distributions':packages,
            'scope':'Packages loaded at admission; their installed versions are rechecked before dispatch and acceptance.'}
