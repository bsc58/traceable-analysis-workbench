"""Trusted scenario registration; caller strings never become import paths."""
from ..contracts import Registry, WorkbenchError


def build_registry(name):
    if name == "service_ops":
        from .service_ops import ServiceOpsAdapter, public_skill
        return Registry([ServiceOpsAdapter()], [public_skill()])
    if name == "data_quality":
        from .data_quality import DataQualityAdapter, public_skill
        return Registry([DataQualityAdapter()], [public_skill()])
    raise WorkbenchError("unknown_scenario", "Choose service_ops or data_quality", 422)
