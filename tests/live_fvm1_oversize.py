"""Demonstrate what the report shows for an OVERSIZE artifact.

A >500 MB workspace file (the real FILE_CAP) is impractical to upload here, and a >10 MB
notebook cannot exist at all (the API rejects the import). So we exercise the SAME code path by
temporarily lowering the caps, with real 60 MB / 120 MB files on the workspace. The rows this
produces are byte-for-byte what a genuine >500 MB file would produce in production.
"""
import json,os,subprocess,tempfile,time
import src.exporters.content_fetcher as cf
# real caps: NOTEBOOK 10MB / FILE 500MB. Lower FILE so the 60MB+120MB fixtures trip it.
cf.FILE_CAP = 50*1024*1024
from src.auth.token_manager import ApiClient
from src.config.config_manager import Config
from src.collectors.inventory_runner import InventoryRunner
from src.exporters.artifact_writer import ArtifactWriter
from src.exporters.export_runner import ExportRunner
import configparser
c=configparser.ConfigParser(); c.read(os.path.expanduser("~/.databrickscfg"))
host=dict(c["fvm1"])["host"].rstrip("/"); wsid=dict(c["fvm1"])["workspace_id"]
def tok(): return json.loads(subprocess.check_output(["databricks","auth","token","-p","fvm1"],text=True))["access_token"]
st=tempfile.mkdtemp(prefix="wsmig_oversize_")
cfg=Config.from_dict({"role":"source","source_workspace_id":wsid,"run_id":"oversize","source_staging_location":st})
cfg.ctx.workspace_url=host; cfg.ctx.token=tok()
cache={"t":cfg.ctx.token,"ts":time.time()}
def tp():
    if time.time()-cache["ts"]>500: cache["t"]=tok(); cache["ts"]=time.time()
    return cache["t"]
client=ApiClient(host,tp); aw=ArtifactWriter(cfg)
InventoryRunner(client,cfg,aw).run()
res=ExportRunner(client,cfg,aw,content_fetch_workers=8).run()
print("\nsummary:",{k:res.get(k) for k in ("total","success","failure","skipped_oversize","manual","dab")})
idx=json.load(open(f"{aw.root}/misc/export_index.json"))
print("\n=== OVERSIZE units in the ledger ===")
for u in idx["units"]:
    if u["export_status"]=="skipped_oversize":
        print(f"  {u['asset_type']:15} {u['natural_key'][-52:]}")
        print(f"      note: {u.get('note','')[:150]}")
print("\n=== oversize_artifacts.json ===")
print(json.dumps(json.load(open(f"{aw.root}/export/oversize_artifacts.json")),indent=1)[:1200])
print("\n=== manual_actions.md (oversize section) ===")
md=open(f"{aw.root}/export/manual/manual_actions.md").read()
i=md.find("## oversize")
print(md[i:i+500] if i>=0 else "(no oversize section)")
print("\nbundle:",aw.root)
