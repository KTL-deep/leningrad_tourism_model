#!/usr/bin/env python3
"""Local manual task metrics; no API, telemetry or credential access."""
import argparse, csv, datetime, pathlib, statistics
FIELDS=['date','task','route','result','minutes','attempts','notes']
p=argparse.ArgumentParser();sub=p.add_subparsers(dest='command',required=True)
a=sub.add_parser('add');
for k in ('task','route','result','minutes','attempts','notes'):a.add_argument('--'+k,required=k in ('task','route','result'),default='')
sub.add_parser('report');args=p.parse_args()
base=pathlib.Path.cwd()/'.codex-local';file=base/'usage.csv'
if args.command=='add':
 base.mkdir(exist_ok=True);new=not file.exists()
 with file.open('a',newline='',encoding='utf-8-sig') as f:
  w=csv.DictWriter(f,fieldnames=FIELDS)
  if new:w.writeheader()
  w.writerow({'date':datetime.date.today().isoformat(),**{k:getattr(args,k) for k in FIELDS[1:]}})
 print('Added:',file)
else:
 if not file.exists():print('No log yet');raise SystemExit(0)
 with file.open(newline='',encoding='utf-8-sig') as f:rows=list(csv.DictReader(f))
 print('Tasks:',len(rows),'passed:',sum(x['result'].lower()=='pass' for x in rows))
 for route in sorted(set(x['route'] for x in rows)):
  chunk=[x for x in rows if x['route']==route]
  nums=[float(x['minutes']) for x in chunk if x['minutes']]
  print(f'{route}: {len(chunk)} tasks; pass {sum(x["result"].lower()=="pass" for x in chunk)}; avg minutes {round(statistics.mean(nums),1) if nums else "n/a"}')
