"""Local startup admission, independent of source/business connections."""
from sqlalchemy import text
from .contracts import WorkbenchError


def check_metadata_access(store, database, role, *, legacy_readonly=False):
    with store.engine.connect() as c:
        identity = c.execute(text("SELECT current_database(), current_user, "
            "(SELECT rolsuper FROM pg_roles WHERE rolname=current_user), "
            "current_setting('default_transaction_read_only')")).one()
    if legacy_readonly:
        if identity[0] != 'workbench_meta' or identity[3] != 'on':
            raise WorkbenchError('metadata_scope_mismatch', 'Legacy mode requires the readonly archive', 403)
    elif identity[0] != database or identity[1] != role or identity[2]:
        raise WorkbenchError('metadata_scope_mismatch', 'Use the designated database and non-superuser application role', 403)
