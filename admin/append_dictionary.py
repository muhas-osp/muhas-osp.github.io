#!/usr/bin/env python3
"""Append rows (e.g. staff_digest_fields.csv) to a data dictionary downloaded from REDCap.

  python admin/append_dictionary.py MUHASStaffList_DataDictionary_....csv admin/staff_digest_fields.csv -o staff_dictionary_with_digest.csv
Fields already present are not duplicated. Upload the output in Project Setup > Data Dictionary.
"""
import argparse, csv
ap = argparse.ArgumentParser(); ap.add_argument("current"); ap.add_argument("extra"); ap.add_argument("-o", "--output", required=True)
a = ap.parse_args()
cur = list(csv.reader(open(a.current, encoding="utf-8-sig")))
have = {r[0] for r in cur[1:] if r}
extra = [r for r in list(csv.reader(open(a.extra, encoding="utf-8-sig")))[1:] if r and r[0] not in have]
with open(a.output, "w", newline="", encoding="utf-8") as f:
    csv.writer(f, quoting=csv.QUOTE_ALL).writerows(cur + extra)
print(f"{len(extra)} field(s) added -> {a.output}")
