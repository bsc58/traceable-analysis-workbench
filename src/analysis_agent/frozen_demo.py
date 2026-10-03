"""Queryable frozen synthetic commerce, retaining the live tool contracts."""
import pyarrow as pa
from .demo import CommerceAdapter
from .contracts import digest


def freeze_commerce(manager,adapter,hook=lambda p:None):
    key,snapshot=manager.stage()
    return manager.publish(key,snapshot,{'orders':[pa.Table.from_pylist(list(adapter._orders))],
        'refunds':[pa.Table.from_pylist(list(adapter._refunds))]},metadata={'source_catalog':adapter.catalog(),'origin':'independent_public_synthetic'},hook=hook)


class FrozenCommerceAdapter(CommerceAdapter):
    def __init__(self,snapshot):
        super().__init__();self.snapshot=snapshot;self._catalog=snapshot.manifest['metadata']['source_catalog']
    @property
    def ref(self):return 'src_'+digest(self.catalog())+'@f1'
    def capabilities(self):return {**super().capabilities(),'live':False,'frozen':True}
    def catalog(self):
        return {**self._catalog,'capabilities':self.capabilities(),'data_version':self.data_version(),
            'limitations':['Public synthetic frozen query; as_known_at_supported=false.']}
    def data_version(self):return self.snapshot.data_version()
    def validate_task(self,parameters):
        super().validate_task(parameters);self.snapshot.manager.open(self.snapshot.key)
    def execute(self,tool_ref,args,parameters):
        self.validate_call(tool_ref,args,parameters)
        # A new DuckDB query actually reads verified frozen rows on every call.
        with self.snapshot.connection() as con:
            orders=con.execute('SELECT * FROM orders').fetch_arrow_table().to_pylist()
            refunds=con.execute('SELECT * FROM refunds').fetch_arrow_table().to_pylist()
        local=CommerceAdapter();local._orders=tuple(orders);local._refunds=tuple(refunds)
        result=local.execute(tool_ref,args,parameters)
        return result.model_copy(update={'warnings':['Frozen synthetic data; no point-in-time availability claim.'],
            'provenance':{**result.provenance,'source_ref':self.ref,'consistency':'frozen','data_version':self.data_version(),'source_queries_executed':2}})
