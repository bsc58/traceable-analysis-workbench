"""Bounded Parquet versions. Only trusted operators supply paths or SQL."""
from __future__ import annotations
import base64, hashlib, json, os, re, uuid
from pathlib import Path
import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import insert, select, update
from .contracts import WorkbenchError, canonical, digest, now
from .storage import dataset_versions, snapshot_sets, Objects
from .path_policy import runtime_path


def checksum(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def sync_dir(path):
    fd=os.open(path,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)


class Snapshots:
    def __init__(self, store, root:Path, privacy):
        self.store=store;self.root=runtime_path(root);self.privacy=privacy
        Objects.initialize(self.root,privacy);Objects.check_class(self.root,privacy)
        self.root.chmod(0o700)

    def stage(self):
        key='ds_'+uuid.uuid4().hex; snapshot='ss_'+uuid.uuid4().hex
        with self.store.engine.begin() as c:
            c.execute(insert(dataset_versions).values(id=key,privacy=self.privacy,state='STAGING',created_at=now()))
            c.execute(insert(snapshot_sets).values(id=snapshot,dataset_id=key,state='STAGING',created_at=now()))
        folder=self.root/key;folder.mkdir(mode=0o700)
        return key,snapshot

    def publish(self,key,snapshot,tables,*,metadata,hook=lambda p:None):
        """tables maps registered names to iterables of Arrow batches/tables.

        Failure leaves STAGING and never publishes a partial version. No implicit
        resume or delete; retained staging artifacts are inspectable by operators.
        """
        with self.store.engine.connect() as c:
            ds=c.execute(select(dataset_versions).where(dataset_versions.c.id==key)).mappings().one()
            ss=c.execute(select(snapshot_sets).where(snapshot_sets.c.id==snapshot)).mappings().one()
        if ds['state']!='STAGING' or ss['state']!='STAGING' or ss['dataset_id']!=key or ds['privacy']!=self.privacy:
            raise WorkbenchError('snapshot_state','Snapshot cannot be published',409)
        folder=runtime_path(self.root/key);files={}
        for name,batches in tables.items():
            if not re.fullmatch('[a-z][a-z0-9_]*',name):raise ValueError('Invalid registered table')
            part=folder/(name+'.staging'); target=folder/(name+'.parquet');writer=None;count=0;schema=None
            # exclusive creation prevents concurrent publication to one stage
            with part.open('xb') as output:
                try:
                    for batch in batches:
                        if writer is None:schema=batch.schema;writer=pq.ParquetWriter(output,schema,compression='zstd')
                        writer.write_table(batch) if isinstance(batch,pa.Table) else writer.write_batch(batch)
                        count+=batch.num_rows;hook('after_batch')
                    if writer is None:raise WorkbenchError('snapshot_empty_schema','An explicit schema is required even for empty data')
                finally:
                    if writer:writer.close()
                output.flush();os.fsync(output.fileno())
            os.link(part,target);part.unlink();target.chmod(0o400)
            # Full decode, not just footer availability, is required before publication.
            decoded=pq.read_table(target)
            if decoded.num_rows!=count or decoded.schema!=schema:raise WorkbenchError('snapshot_corrupt','Parquet verification failed',409)
            files[name]={'file':target.name,'rows':count,'bytes':target.stat().st_size,'sha256':checksum(target),'schema':str(schema)}
        hook('before_publish')
        manifest={'schema_version':'dataset_manifest@1','dataset_id':key,'snapshot_set_id':snapshot,'privacy':self.privacy,'files':files,'metadata':metadata,'as_known_at_supported':False,'created_at':now()}
        body=canonical(manifest);path=folder/'manifest.json'
        with path.open('xb') as f:f.write(body);f.flush();os.fsync(f.fileno())
        path.chmod(0o400);sync_dir(folder)
        with self.store.engine.begin() as c:
            ds=c.execute(select(dataset_versions).where(dataset_versions.c.id==key).with_for_update()).mappings().one()
            if ds['state']!='STAGING':raise WorkbenchError('snapshot_state','Snapshot state changed',409)
            c.execute(update(dataset_versions).where(dataset_versions.c.id==key).values(state='AVAILABLE',manifest=manifest))
            c.execute(update(snapshot_sets).where(snapshot_sets.c.id==snapshot).values(state='AVAILABLE',manifest_hash=hashlib.sha256(body).hexdigest()))
        return self.open(snapshot)

    def open(self,snapshot):
        with self.store.engine.connect() as c:
            row=c.execute(select(snapshot_sets,dataset_versions.c.privacy,dataset_versions.c.manifest,dataset_versions.c.state.label('dataset_state')).join(dataset_versions,dataset_versions.c.id==snapshot_sets.c.dataset_id).where(snapshot_sets.c.id==snapshot)).mappings().first()
        if not row or row['state']!='AVAILABLE' or row['dataset_state']!='AVAILABLE' or row['privacy']!=self.privacy:
            raise WorkbenchError('snapshot_unavailable','Snapshot is not AVAILABLE in this store',409)
        folder=runtime_path(self.root/row['dataset_id']);path=folder/'manifest.json'
        if not path.is_file() or checksum(path)!=row['manifest_hash'] or digest(row['manifest'])!=row['manifest_hash']:
            raise WorkbenchError('snapshot_corrupt','Snapshot manifest mismatch',409)
        return Snapshot(self,snapshot,row['manifest'],row['manifest_hash'])

    def retire(self,snapshot,state='RETIRED'):
        if state not in {'RETIRED','EXPIRED'}:raise ValueError('Invalid final state')
        with self.store.engine.begin() as c:
            row=c.execute(select(snapshot_sets).where(snapshot_sets.c.id==snapshot).with_for_update()).mappings().one()
            if row['state']!='AVAILABLE':raise WorkbenchError('snapshot_state','Only AVAILABLE snapshots can retire',409)
            c.execute(update(snapshot_sets).where(snapshot_sets.c.id==snapshot).values(state=state))
            c.execute(update(dataset_versions).where(dataset_versions.c.id==row['dataset_id']).values(state=state))


class Snapshot:
    def __init__(self,manager,key,manifest,manifest_hash):
        self.manager=manager;self.key=key;self.manifest=manifest;self.manifest_hash=manifest_hash
    def data_version(self):
        return {'mode':'frozen','dataset_version':self.manifest['dataset_id'],'snapshot_set':self.key,'manifest_sha256':self.manifest_hash,'queryable_snapshot':True,'point_in_time_reconstruction':False,'as_known_at_supported':False}
    def connection(self):
        self.manager.open(self.key) # Reject retired/expired versions before every new read.
        con=duckdb.connect(':memory:',config={'enable_external_access':False,'threads':1,'memory_limit':'512MB','allow_unsigned_extensions':False})
        try:
            for name,info in self.manifest['files'].items():
                path=runtime_path(self.manager.root/self.manifest['dataset_id']/info['file'])
                if path.parent!=self.manager.root/self.manifest['dataset_id'] or path.is_symlink():raise WorkbenchError('snapshot_corrupt','Snapshot path mismatch',409)
                data=path.read_bytes()
                if hashlib.sha256(data).hexdigest()!=info['sha256']:raise WorkbenchError('snapshot_corrupt','Snapshot file checksum mismatch',409)
                # Arrow decodes the verified bytes; DuckDB never opens an external file.
                table=pq.read_table(pa.BufferReader(data));con.register(name,table)
            con.execute('SET lock_configuration=true')
            return con
        except BaseException:con.close();raise
    def cursor(self,offset,query):
        return base64.urlsafe_b64encode(canonical({'snapshot':self.key,'manifest':self.manifest_hash,'offset':offset,'query':digest(query)})).decode()
    def offset(self,cursor,query):
        try:
            value=json.loads(base64.b64decode(cursor,altchars=b'-_',validate=True))
            assert value['snapshot']==self.key and value['manifest']==self.manifest_hash and value['query']==digest(query)
            assert type(value['offset']) is int and value['offset']>=0
            return value['offset']
        except (ValueError,KeyError,AssertionError,TypeError):raise WorkbenchError('cursor_mismatch','Cursor belongs to another snapshot or query',409) from None
