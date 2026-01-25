"""
Docstring for lmdb.cli

A script to test out xform functions
"""

import datetime
from . import models, xform

DOCO = ''

with open(DOCO, encoding='utf-8') as f:
    dlp = models.PlaylistDLP.model_validate_json(f.read())

print(dlp)

native = xform.pl_dlp2lm(dlp)

print(native)

summary = xform.full2sum(native)

print(summary)

stats = xform.full2stats(native, 1)

print(stats)

sched = models.PlaylistSched(extractor_id=summary.extractor_id,
                             id=summary.id,
                             next_run=datetime.date.today(),
                             freq_days=0,
                             input_prams={},
                             webpage_url=summary.webpage_url,
                             sched_id=1)

sched, existing, stats = xform.add_new_run(sched, [], stats)

print(sched)
print(existing)
print(stats)
