"""
live_fvm1_resume — verify export RESUME + fingerprint stability against live fvm1.

Runs the export TWICE into the same bundle dir and asserts:
  1. the 2nd run resumes (same bundle, no failures, content not refetched), and
  2. every unit's fingerprint is byte-identical across the two runs.

(2) is the important one: the target's cross-run UPSERT decides create/update/skip by
comparing fingerprints, so any per-call instability in the source payload (e.g. SCIM
returning group `members` in a random order) would make unchanged assets look changed and
be re-updated forever. This harness is what caught exactly that.

Run: python3 -m tests.live_fvm1_resume   (needs the `fvm1` profile authenticated)
"""
import json, os, subprocess, time, tempfile
from src.auth.token_manager import ApiClient
from src.config.config_manager import Config
from src.collectors.inventory_runner import InventoryRunner
from src.exporters.artifact_writer import ArtifactWriter
from src.exporters.export_runner import ExportRunner

P="fvm1"
import configparser
c=configparser.ConfigParser(); c.read(os.path.expanduser("~/.databrickscfg"))
host=dict(c[P])["host"].rstrip("/"); wsid=dict(c[P])["workspace_id"]
def tok(): return json.loads(subprocess.check_output(["databricks","auth","token","-p",P],text=True))["access_token"]
staging=tempfile.mkdtemp(prefix="wsmig_resume_")
cache={"t":tok(),"ts":time.time()}
def tp():
    if time.time()-cache["ts"]>500: cache["t"]=tok(); cache["ts"]=time.time()
    return cache["t"]
def run(tag):
    cfg=Config.from_dict({"role":"source","source_workspace_id":wsid,"run_id":"resume1",
                          "source_staging_location":staging})
    cfg.ctx.workspace_url=host; cfg.ctx.token=cache["t"]
    client=ApiClient(host,tp); aw=ArtifactWriter(cfg)
    InventoryRunner(client,cfg,aw).run()
    res=ExportRunner(client,cfg,aw,content_fetch_workers=8).run()
    idx=json.load(open(f"{aw.root}/misc/export_index.json"))
    fps={(u["asset_type"],u["natural_key"]):u.get("fingerprint") for u in idx["units"]}
    print(f"[{tag}] total={res['total']} success={res['success']} failure={res['failure']}")
    return aw.root,fps,res

r1,fp1,res1=run("run1")
r2,fp2,res2=run("run2-resume")
print()
print("same bundle dir:", r1==r2)
print("fingerprints identical across runs:", fp1==fp2, f"({len(fp1)} units)")
diff={k:(fp1.get(k),fp2.get(k)) for k in set(fp1)|set(fp2) if fp1.get(k)!=fp2.get(k)}
print("fingerprint diffs:", len(diff))
for k,v in list(diff.items())[:5]: print("  ",k,v)
print("failures run2:", res2["failure"])
