#!/usr/bin/env python3
"""Item-value skew analyzer for the LOTRLOME_Armory module (READ-ONLY) -- #318.

#318 asks a decision question: are LOTRLOME's computed item values (~2.2x vanilla)
intended, or should they be normalized? It cannot be answered without numbers, and
it explicitly asks for "its own analyzer" before any apply. This is that analyzer.

WHAT IT MEASURES, AND WHAT IT DOES NOT
--------------------------------------
None of LOTRLOME's items ship an explicit `value=`, so the engine derives each one
from the item's stats via `DefaultItemValueModel`. This tool does **not** reimplement
that model. Guessing at an engine formula would produce authoritative-looking numbers
nobody can check, and the decision #318 needs does not depend on it.

What it does instead:

  * **Stat skew, measured.** Per armor type, the median/p90/max of the governing armor
    stat and weight, for LOTRLOME against vanilla SandBoxCore, with the ratio. Stats are
    the input the value model keys off, so this is the driver of the price gap and it is
    objective -- no formula involved.

  * **Explicit-value coverage.** How many items in each tree carry `value=`. This is the
    root of the whole issue: 0% in LOTRLOME means every price is derived.

  * **The outliers worth acting on first.** Items whose governing stat sits above the
    highest vanilla item of the same type -- #318 calls out leg armor as the worst skew,
    and this names the individual items behind it.

  * **Actual engine values, when supplied.** `--values-csv id,value` folds in values read
    from a running game (or any other source) and reports the real value ratio per type
    alongside the stat ratio. Without it the report says so rather than inferring.

Hero/boss/display items are excluded from the curve statistics for the same reason
`analyze_armor_balance.py` excludes them: they are deliberately extreme and would drag
every median. They are still counted and listed separately.

This script writes nothing but its own report under tools/reports/item-values/.

Usage:
    python tools/analyze_item_values.py
    python tools/analyze_item_values.py --stdout
    python tools/analyze_item_values.py --values-csv measured.csv
    python tools/analyze_item_values.py --armory-path "C:\\Games\\LOTRAOM\\patreon\\Modules\\LOTRLOME_Armory\\ModuleData"
"""
import argparse
import csv
import json
import os
import re
import statistics
import sys
import datetime
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _gamedir import game_dir, ensure_exists  # noqa: E402

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
REPORT_DIR = os.path.join(REPO_ROOT, 'tools', 'reports', 'item-values')

# Same literal every other tool here carries; $BANNERLORD_GAME_DIR overrides it (#404).
DEFAULT_GAME_DIR = r"E:\Steam\steamapps\common\Mount & Blade II Bannerlord"

# The engine's own classification (the `Type` attribute), not the filename: the two trees
# organise their files differently, and Type is what `DefaultItemValueModel` switches on.
GOVERNING_STAT = {
    'HeadArmor': 'head_armor',
    'BodyArmor': 'body_armor',
    'Cape': 'body_armor',
    'HandArmor': 'arm_armor',
    'LegArmor': 'leg_armor',
}

# Same exclusion intent as analyze_armor_balance.py: named characters and display pieces are
# deliberately off-curve, so they are reported but never allowed to move a median.
EXCLUDE_PATTERNS = re.compile(
    r'(_hero|hero_|boss|display|unique|_npc|dummy|test_|debug)', re.IGNORECASE)

ITEM_RE = re.compile(r'<Item\b([^>]*?)(?:/>|>(.*?)</Item>)', re.S)
ARMOR_RE = re.compile(r'<Armor\b([^>]*?)/?>', re.S)


def _attr(text, name):
    m = re.search(r'\b%s="([^"]*)"' % re.escape(name), text)
    return m.group(1) if m else None


def parse_tree(root):
    """Every <Item> under `root`, as flat dicts. Regex rather than ElementTree on purpose:
    several shipped armor files carry malformed fragments that make a strict parse throw,
    and a tool that dies on one bad file reports nothing about the other 3,296."""
    items = []
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if not fn.lower().endswith('.xml'):
                continue
            path = os.path.join(dirpath, fn)
            try:
                with open(path, encoding='utf-8', errors='ignore') as fh:
                    text = fh.read()
            except OSError:
                continue
            for m in ITEM_RE.finditer(text):
                attrs, body = m.group(1) or '', m.group(2) or ''
                item_type = _attr(attrs, 'Type')
                if item_type not in GOVERNING_STAT:
                    continue
                armor = ARMOR_RE.search(body)
                stat = None
                if armor:
                    raw = _attr(armor.group(1), GOVERNING_STAT[item_type])
                    if raw is not None:
                        try:
                            stat = int(float(raw))
                        except ValueError:
                            stat = None
                item_id = _attr(attrs, 'id') or ''
                weight = _attr(attrs, 'weight')
                items.append({
                    'id': item_id,
                    'name': _attr(attrs, 'name') or '',
                    'type': item_type,
                    'culture': (_attr(attrs, 'culture') or '').replace('Culture.', ''),
                    'stat': stat,
                    'weight': float(weight) if weight else None,
                    'has_explicit_value': _attr(attrs, 'value') is not None,
                    'excluded': bool(EXCLUDE_PATTERNS.search(item_id)),
                    'file': os.path.relpath(path, root),
                })
    return items


def _summary(values):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    return {
        'n': len(vals),
        'median': statistics.median(vals),
        'p90': vals[max(0, int(0.9 * len(vals)) - 1)],
        'max': vals[-1],
    }


def compare(taom_items, vanilla_items, measured=None):
    """Per armor type: the stat curve on each side, the ratio, and (if supplied) real values."""
    rows = []
    for item_type in GOVERNING_STAT:
        t = [i for i in taom_items if i['type'] == item_type and not i['excluded']]
        v = [i for i in vanilla_items if i['type'] == item_type and not i['excluded']]
        ts, vs = _summary([i['stat'] for i in t]), _summary([i['stat'] for i in v])
        row = {
            'type': item_type,
            'stat': GOVERNING_STAT[item_type],
            'taom': ts,
            'vanilla': vs,
            'stat_ratio': (round(ts['median'] / vs['median'], 2)
                           if ts and vs and vs['median'] else None),
            'taom_excluded': sum(1 for i in taom_items
                                 if i['type'] == item_type and i['excluded']),
        }
        if measured:
            tv = _summary([measured.get(i['id']) for i in t])
            vv = _summary([measured.get(i['id']) for i in v])
            row['taom_value'] = tv
            row['vanilla_value'] = vv
            row['value_ratio'] = (round(tv['median'] / vv['median'], 2)
                                  if tv and vv and vv['median'] else None)
        rows.append(row)
    return rows


def outliers(taom_items, vanilla_items, limit=15):
    """LOTRLOME items whose governing stat exceeds the highest vanilla item of that type.

    Above the vanilla ceiling is the defensible line to act on first: it needs no opinion
    about the right curve, only the observation that nothing in the base game reaches it.
    """
    ceiling = {}
    for item_type in GOVERNING_STAT:
        vs = [i['stat'] for i in vanilla_items
              if i['type'] == item_type and not i['excluded'] and i['stat'] is not None]
        ceiling[item_type] = max(vs) if vs else None

    over = []
    for i in taom_items:
        cap = ceiling.get(i['type'])
        if i['excluded'] or cap is None or i['stat'] is None or i['stat'] <= cap:
            continue
        over.append({**i, 'vanilla_max': cap, 'over_by': i['stat'] - cap,
                     'times': round(i['stat'] / cap, 2) if cap else None})
    over.sort(key=lambda r: -r['over_by'])
    return ceiling, over[:limit], len(over)


def load_measured(path):
    """id,value CSV -- values read from a running game, or any other source."""
    out = {}
    with open(path, encoding='utf-8', newline='') as fh:
        for row in csv.reader(fh):
            if len(row) < 2:
                continue
            try:
                out[row[0].strip()] = float(row[1])
            except ValueError:
                continue          # header line or junk
    return out


def render(rows, ceiling, top, over_total, taom_items, vanilla_items, measured, paths):
    stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    taom_expl = sum(1 for i in taom_items if i['has_explicit_value'])
    van_expl = sum(1 for i in vanilla_items if i['has_explicit_value'])
    L = []
    L.append('# LOTRLOME item-value skew (#318)')
    L.append('')
    L.append(f'Generated {stamp}. Read-only.')
    L.append('')
    L.append(f'- LOTRLOME armor items: **{len(taom_items)}**, '
             f'with an explicit `value=`: **{taom_expl}** '
             f'({100 * taom_expl / max(len(taom_items), 1):.0f}%)')
    L.append(f'- Vanilla armor items: **{len(vanilla_items)}**, '
             f'with an explicit `value=`: **{van_expl}** '
             f'({100 * van_expl / max(len(vanilla_items), 1):.0f}%)')
    L.append('')
    L.append(f'- LOTRLOME tree: `{paths["armory"]}`')
    L.append(f'- Vanilla tree: `{paths["vanilla"]}`')
    L.append('')
    L.append('## Stat curve by armor type')
    L.append('')
    L.append('Governing stat only, hero/display items excluded. This is the input the engine '
             'derives price from; it is measured, not modelled.')
    L.append('')
    L.append('| type | stat | LOTRLOME n | median | p90 | max | vanilla n | median | p90 | max | '
             'median ratio |')
    L.append('|---|---|---|---|---|---|---|---|---|---|---|')
    for r in rows:
        t, v = r['taom'], r['vanilla']
        if not t or not v:
            continue
        L.append(f"| {r['type']} | `{r['stat']}` | {t['n']} | {t['median']:.0f} | {t['p90']} | "
                 f"{t['max']} | {v['n']} | {v['median']:.0f} | {v['p90']} | {v['max']} | "
                 f"**{r['stat_ratio']}x** |")
    L.append('')
    if measured:
        L.append('## Actual engine values, from the supplied measurements')
        L.append('')
        L.append('| type | LOTRLOME n | median value | vanilla n | median value | ratio |')
        L.append('|---|---|---|---|---|---|')
        for r in rows:
            tv, vv = r.get('taom_value'), r.get('vanilla_value')
            if not tv or not vv:
                continue
            L.append(f"| {r['type']} | {tv['n']} | {tv['median']:.0f} | {vv['n']} | "
                     f"{vv['median']:.0f} | **{r['value_ratio']}x** |")
        L.append('')
    else:
        L.append('## Actual engine values')
        L.append('')
        L.append('Not supplied. This tool does not reimplement `DefaultItemValueModel`; pass '
                 '`--values-csv id,value` with values read from a running game to get the real '
                 'price ratio alongside the stat ratio above.')
        L.append('')
    L.append('## Above the vanilla ceiling')
    L.append('')
    L.append(f'**{over_total}** LOTRLOME items carry a governing stat higher than any vanilla '
             'item of the same type. Ceilings: '
             + ', '.join(f'{k} {v}' for k, v in ceiling.items() if v is not None) + '.')
    L.append('')
    if top:
        L.append('| item | type | stat | vanilla max | over by | x |')
        L.append('|---|---|---|---|---|---|')
        for r in top:
            L.append(f"| `{r['id']}` | {r['type']} | {r['stat']} | {r['vanilla_max']} | "
                     f"+{r['over_by']} | {r['times']}x |")
        L.append('')
    L.append('## What this does not answer')
    L.append('')
    L.append('- Whether the skew is intended. That is a design call, and #318 frames it as one.')
    L.append('- The knock-on effects #318 lists (battle reward shares, upgrade costs, buy '
             'prices) all key off the same derived value, so any change moves them together.')
    return '\n'.join(L) + '\n'


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--armory-path', help='LOTRLOME_Armory ModuleData directory')
    ap.add_argument('--vanilla-path', help='SandBoxCore items directory')
    ap.add_argument('--game-dir', help='Bannerlord install root (overrides BANNERLORD_GAME_DIR)')
    ap.add_argument('--values-csv', help='id,value pairs measured from a running game')
    ap.add_argument('--stdout', action='store_true', help='also print the report')
    ap.add_argument('--json', dest='json_out', help='write the JSON sidecar here')
    args = ap.parse_args()

    root = args.game_dir or game_dir(DEFAULT_GAME_DIR)
    # The two trees can live in different installs — LOTRAOM ships as its own copy of the
    # game — so each takes its own override rather than both hanging off one root.
    armory = args.armory_path or os.path.join(
        root, 'Modules', 'LOTRLOME_Armory', 'ModuleData')
    vanilla = args.vanilla_path or os.path.join(
        root, 'Modules', 'SandBoxCore', 'ModuleData', 'items')

    ensure_exists(armory, 'the LOTRLOME_Armory ModuleData directory')
    ensure_exists(vanilla, 'the vanilla SandBoxCore items directory')

    taom_items = parse_tree(armory)
    vanilla_items = parse_tree(vanilla)
    if not taom_items:
        print(f'ERROR: no armor items found under {armory}', file=sys.stderr)
        return 2
    if not vanilla_items:
        print(f'ERROR: no armor items found under {vanilla}', file=sys.stderr)
        return 2

    measured = load_measured(args.values_csv) if args.values_csv else None
    rows = compare(taom_items, vanilla_items, measured)
    ceiling, top, over_total = outliers(taom_items, vanilla_items)

    report = render(rows, ceiling, top, over_total, taom_items, vanilla_items, measured,
                    {'armory': armory, 'vanilla': vanilla})

    os.makedirs(REPORT_DIR, exist_ok=True)
    md_path = os.path.join(REPORT_DIR, 'REPORT.md')
    with open(md_path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(report)
    json_path = args.json_out or os.path.join(REPORT_DIR, 'report.json')
    with open(json_path, 'w', encoding='utf-8', newline='\n') as fh:
        json.dump({'types': rows, 'ceiling': ceiling, 'outliers': top,
                   'outliers_total': over_total,
                   'counts': {'taom': len(taom_items), 'vanilla': len(vanilla_items)}},
                  fh, indent=2)

    print(f'Wrote {os.path.relpath(md_path, REPO_ROOT)} '
          f'({len(taom_items)} LOTRLOME vs {len(vanilla_items)} vanilla armor items, '
          f'{over_total} above the vanilla ceiling)')
    if args.stdout:
        print()
        print(report)
    return 0


if __name__ == '__main__':
    sys.exit(main())
