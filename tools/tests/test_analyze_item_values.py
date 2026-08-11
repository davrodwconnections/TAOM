#!/usr/bin/env python3
"""Tests for tools/analyze_item_values.py (#318).

Run:  python -m unittest discover -s tools/tests -p "test_*.py"

The analyzer exists to inform a balance decision, so the properties that matter are the
ones that would make it quietly mislead rather than the ones that would make it crash:

  * **The exclusion has to actually move a number.** A hero-item filter that changes no
    median is indistinguishable from no filter at all, so the exclusion test asserts the
    unfiltered median differs — otherwise it would pass with the filter deleted.
  * **A stat ratio must never be read as a price ratio.** The tool does not model
    `DefaultItemValueModel`; the report has to say so when no measurements are supplied.
  * **One malformed file must not silence the other few thousand items.** Several shipped
    armor files carry fragments a strict XML parse rejects.

No test reads the real game install.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import analyze_item_values as aiv  # noqa: E402


def _item(item_id, item_type, stat_name, stat, extra_attrs=''):
    return (f'<Item id="{item_id}" name="{item_id}" Type="{item_type}"{extra_attrs}>'
            f'<ItemComponent><Armor {stat_name}="{stat}" /></ItemComponent></Item>')


def _tree(tmp, name, items):
    d = os.path.join(tmp, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, 'armors.xml'), 'w', encoding='utf-8') as fh:
        fh.write('<Items>\n' + '\n'.join(items) + '\n</Items>')
    return d


class Parsing(unittest.TestCase):

    def test_the_governing_stat_is_read_per_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _tree(tmp, 'a', [
                _item('helm', 'HeadArmor', 'head_armor', 40),
                _item('cuirass', 'BodyArmor', 'body_armor', 50),
                _item('greaves', 'LegArmor', 'leg_armor', 20),
            ])
            got = {i['id']: i['stat'] for i in aiv.parse_tree(d)}
        self.assertEqual({'helm': 40, 'cuirass': 50, 'greaves': 20}, got)

    def test_types_with_no_governing_armor_stat_are_left_out(self):
        # Shields, bows and horses live in the same tree and have no armor curve.
        with tempfile.TemporaryDirectory() as tmp:
            d = _tree(tmp, 'a', [
                _item('helm', 'HeadArmor', 'head_armor', 40),
                '<Item id="shield" Type="Shield"><ItemComponent /></Item>',
                '<Item id="nag" Type="Horse"><ItemComponent /></Item>',
            ])
            ids = [i['id'] for i in aiv.parse_tree(d)]
        self.assertEqual(['helm'], ids)

    def test_a_malformed_file_does_not_hide_the_rest(self):
        # The reason this is regex and not ElementTree: one bad fragment used to mean the
        # tool reported nothing at all about the other thousands of items.
        with tempfile.TemporaryDirectory() as tmp:
            d = _tree(tmp, 'a', [_item('helm', 'HeadArmor', 'head_armor', 40)])
            with open(os.path.join(d, 'broken.xml'), 'w', encoding='utf-8') as fh:
                fh.write('<Items><Item id="oops" Type="HeadArmor"><unclosed>')
            ids = [i['id'] for i in aiv.parse_tree(d)]
        self.assertIn('helm', ids)

    def test_an_explicit_value_attribute_is_noticed(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _tree(tmp, 'a', [
                _item('priced', 'HeadArmor', 'head_armor', 40, ' value="1234"'),
                _item('derived', 'HeadArmor', 'head_armor', 40),
            ])
            got = {i['id']: i['has_explicit_value'] for i in aiv.parse_tree(d)}
        self.assertEqual({'priced': True, 'derived': False}, got)


class HeroExclusion(unittest.TestCase):

    def test_hero_items_are_excluded_AND_that_changes_the_median(self):
        # The second half is the point. Asserting only "excluded is True" would still pass
        # with the filter removed from every calculation that uses it.
        with tempfile.TemporaryDirectory() as tmp:
            # Distinct stats, and an ODD count before the hero piece is added: with three
            # identical values the median stays put whether the filter runs or not, and the
            # test would pass with the exclusion deleted. Picked so it genuinely moves.
            items = [_item(f'plain_{i}', 'BodyArmor', 'body_armor', s)
                     for i, s in enumerate((10, 20, 30))]
            items.append(_item('boss_armor', 'BodyArmor', 'body_armor', 900))
            parsed = aiv.parse_tree(_tree(tmp, 'a', items))

        self.assertTrue(next(i for i in parsed if i['id'] == 'boss_armor')['excluded'])

        kept = aiv._summary([i['stat'] for i in parsed if not i['excluded']])
        everything = aiv._summary([i['stat'] for i in parsed])
        self.assertEqual(30, kept['max'])
        self.assertEqual(900, everything['max'],
                         'the outlier must be present, or this test proves nothing')
        self.assertEqual(20, kept['median'])        # [10, 20, 30]
        self.assertEqual(25, everything['median'])  # [10, 20, 30, 900]
        self.assertNotEqual(kept['median'], everything['median'])


class Ratios(unittest.TestCase):

    def _two_trees(self, tmp, taom_stats, vanilla_stats):
        t = aiv.parse_tree(_tree(tmp, 'taom', [
            _item(f't{i}', 'BodyArmor', 'body_armor', s) for i, s in enumerate(taom_stats)]))
        v = aiv.parse_tree(_tree(tmp, 'van', [
            _item(f'v{i}', 'BodyArmor', 'body_armor', s) for i, s in enumerate(vanilla_stats)]))
        return t, v

    def test_the_median_ratio_is_what_it_says(self):
        with tempfile.TemporaryDirectory() as tmp:
            t, v = self._two_trees(tmp, [30, 30, 30], [10, 10, 10])
            row = next(r for r in aiv.compare(t, v) if r['type'] == 'BodyArmor')
        self.assertEqual(3.0, row['stat_ratio'])

    def test_a_type_absent_from_one_side_yields_no_ratio_rather_than_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            t, v = self._two_trees(tmp, [30], [])
            row = next(r for r in aiv.compare(t, v) if r['type'] == 'BodyArmor')
        self.assertIsNone(row['stat_ratio'])


class Outliers(unittest.TestCase):

    def test_only_items_above_the_vanilla_ceiling_are_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = aiv.parse_tree(_tree(tmp, 'taom', [
                _item('under', 'LegArmor', 'leg_armor', 20),
                _item('equal', 'LegArmor', 'leg_armor', 26),
                _item('over', 'LegArmor', 'leg_armor', 40),
            ]))
            v = aiv.parse_tree(_tree(tmp, 'van', [
                _item('v_low', 'LegArmor', 'leg_armor', 10),
                _item('v_top', 'LegArmor', 'leg_armor', 26),
            ]))
            ceiling, top, total = aiv.outliers(t, v)

        self.assertEqual(26, ceiling['LegArmor'])
        self.assertEqual(['over'], [r['id'] for r in top])
        self.assertEqual(1, total)
        self.assertEqual(14, top[0]['over_by'])

    def test_a_vanilla_hero_item_does_not_raise_the_ceiling(self):
        # Otherwise one deliberately extreme vanilla piece would excuse every mod outlier.
        with tempfile.TemporaryDirectory() as tmp:
            t = aiv.parse_tree(_tree(tmp, 'taom', [
                _item('over', 'LegArmor', 'leg_armor', 40)]))
            v = aiv.parse_tree(_tree(tmp, 'van', [
                _item('v_top', 'LegArmor', 'leg_armor', 26),
                _item('v_hero_boots', 'LegArmor', 'leg_armor', 500),
            ]))
            ceiling, top, _ = aiv.outliers(t, v)

        self.assertEqual(26, ceiling['LegArmor'])
        self.assertEqual(['over'], [r['id'] for r in top])


class MeasuredValues(unittest.TestCase):

    def test_junk_rows_are_skipped_and_good_ones_are_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'v.csv')
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write('id,value\nhelm,500\nbroken\ncuirass,not_a_number\ngreaves,250\n')
            got = aiv.load_measured(path)
        self.assertEqual({'helm': 500.0, 'greaves': 250.0}, got)

    def test_the_report_refuses_to_imply_a_price_ratio_without_measurements(self):
        # A reader who takes the stat ratio for a price ratio would misquote this tool in
        # the very issue it was written for.
        with tempfile.TemporaryDirectory() as tmp:
            t, v = (aiv.parse_tree(_tree(tmp, 'taom',
                    [_item('t', 'BodyArmor', 'body_armor', 30)])),
                    aiv.parse_tree(_tree(tmp, 'van',
                    [_item('v', 'BodyArmor', 'body_armor', 10)])))
            rows = aiv.compare(t, v, None)
            ceiling, top, total = aiv.outliers(t, v)
            report = aiv.render(rows, ceiling, top, total, t, v, None,
                                {'armory': 'a', 'vanilla': 'b'})
        self.assertIn('Not supplied', report)
        self.assertIn('does not reimplement', report)

    def test_supplying_measurements_produces_a_value_ratio(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = aiv.parse_tree(_tree(tmp, 'taom',
                                     [_item('t', 'BodyArmor', 'body_armor', 30)]))
            v = aiv.parse_tree(_tree(tmp, 'van',
                                     [_item('v', 'BodyArmor', 'body_armor', 10)]))
            rows = aiv.compare(t, v, {'t': 440.0, 'v': 200.0})
            row = next(r for r in rows if r['type'] == 'BodyArmor')
        self.assertEqual(2.2, row['value_ratio'])


class EndToEnd(unittest.TestCase):
    """main() itself, not just the pieces it calls.

    #444 landed with `--model` accepted, printed and priced but never sent, because every
    test asserted the builder in isolation and nothing exercised the caller. Same shape of
    gap, so the entry point gets its own test.
    """

    def test_main_writes_a_report_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            armory = _tree(tmp, 'armory', [_item('t', 'BodyArmor', 'body_armor', 30)])
            vanilla = _tree(tmp, 'vanilla', [_item('v', 'BodyArmor', 'body_armor', 10)])
            out_json = os.path.join(tmp, 'out.json')
            report_dir = os.path.join(tmp, 'reports')
            original_dir, aiv.REPORT_DIR = aiv.REPORT_DIR, report_dir
            argv = sys.argv
            sys.argv = ['analyze_item_values.py', '--armory-path', armory,
                        '--vanilla-path', vanilla, '--json', out_json]
            try:
                from contextlib import redirect_stdout
                from io import StringIO
                with redirect_stdout(StringIO()):
                    rc = aiv.main()
            finally:
                sys.argv = argv
                aiv.REPORT_DIR = original_dir

            self.assertEqual(0, rc)
            self.assertTrue(os.path.exists(os.path.join(report_dir, 'REPORT.md')))
            self.assertTrue(os.path.exists(out_json))

    def test_a_missing_tree_exits_2_rather_than_reporting_nothing_found(self):
        # `ensure_exists` exists because several tools used to finish clean on a wrong root.
        with tempfile.TemporaryDirectory() as tmp:
            vanilla = _tree(tmp, 'vanilla', [_item('v', 'BodyArmor', 'body_armor', 10)])
            argv = sys.argv
            sys.argv = ['analyze_item_values.py',
                        '--armory-path', os.path.join(tmp, 'nope'),
                        '--vanilla-path', vanilla]
            try:
                from contextlib import redirect_stderr
                from io import StringIO
                err = StringIO()
                with redirect_stderr(err):
                    with self.assertRaises(SystemExit) as ctx:
                        aiv.main()
            finally:
                sys.argv = argv
        self.assertEqual(2, ctx.exception.code)
        self.assertIn('not found', err.getvalue())


if __name__ == '__main__':
    unittest.main()
