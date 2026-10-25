"""Fail CI if the ZAP baseline report has any Medium (2) or High (3) alert whose rule is
not explicitly accepted (IGNORE) in .zap/rules.tsv. Prints every alert either way."""
import json
import sys

report, rules = sys.argv[1], sys.argv[2]
accepted = set()
for line in open(rules):
    parts = line.rstrip("\n").split("\t")
    if len(parts) >= 2 and not line.startswith("#") and parts[1] == "IGNORE":
        accepted.add(parts[0])

data = json.load(open(report))
blocking = []
for site in data.get("site", []):
    for a in site.get("alerts", []):
        risk = int(a.get("riskcode", 0))
        rid = str(a.get("pluginid"))
        tag = "accepted" if rid in accepted else ("BLOCKING" if risk >= 2 else "info")
        print(f"[{tag}] risk={a.get('riskdesc')} id={rid} {a.get('name')} ({a.get('count')} instances)")
        if risk >= 2 and rid not in accepted:
            blocking.append(a.get("name"))
if blocking:
    print(f"\n{len(blocking)} Medium/High ZAP alert(s) not accepted: {blocking}")
    sys.exit(1)
print("\nZAP gate: no unaccepted Medium/High alerts")
