"""Invented deadline-rule fixtures; stable standard-library generator."""
import argparse
import json
from pathlib import Path
import random

SEED = 20260928
RULE = "Lower-case the title, then re.search(regex). Every 'deadline' title must match; every 'not_deadline' title must not."
PATTERN = '\\b(before|by)\\b[^?]*\\b(20\\d\\d|january|february|march|april|may|june|july|august|september|october|november|december)\\b'
ORGANISATIONS = ('Zuvren Circle', 'Velqun Guild', 'Morzivel Club', 'Nuvrasko Hall')
PRODUCTS = ('lumen crate', 'ember spool', 'ribbon pod', 'opal lattice')
MONTHS = ('January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
          'September', 'October', 'November', 'December')


def build():
    rng = random.Random(SEED)
    ids = rng.sample(range(930000, 934000), 159)
    months = ['May'] * 32 + ['September'] * 16 + ['June'] * 12 + ['July'] * 10 + ['August'] * 6 + ['April'] * 5 + ['March'] * 2 + ['January', 'February', 'October', 'November', 'December']
    deadline, other = [], []
    for index, post in enumerate(sorted(ids[:88])):
        organisation, product = rng.choice(ORGANISATIONS), rng.choice(PRODUCTS)
        tag = 'V' + chr(65 + index // 26) + chr(65 + index % 26)
        if index < 14:
            product = ('\u201c' + product + '\u201d') if index < 9 else ('"' + product + '"')
        prefix = '2026 ' if 38 <= index < 52 else ''
        action = 'certify the ' + prefix + product + ' batch ' + tag
        if index in (30, 31):
            action = 'reach 60% yield with its ' + product + ' batch ' + tag
        place = ' at Nolvex trial'
        if 14 <= index < 27:
            place += ' (guild test)'
        if index == 36:
            place = ' between two Nolvex trials'
        keyword = 'by' if index >= 76 else 'before'
        if index == 35:
            keyword = 'on or before'
        year = {3: 2025, 4: 2027, 5: 2038}.get(index, 2026)
        date = months[index] + ' ' + str(8 + index % 20)
        if index >= 3:
            date += ', ' + str(year)
        title = 'Will SYNTHETIC ' + organisation + ' ' + action + place + ' ' + keyword + ' ' + date + '?'
        group = 'spring' if index < 39 else 'summer' if index < 61 else 'minibench'
        deadline.append({'post':post, 'group':group, 'title':title})
    for index, post in enumerate(sorted(ids[88:])):
        organisation, product = rng.choice(ORGANISATIONS), rng.choice(PRODUCTS)
        tag = 'V' + chr(65 + index // 26) + chr(65 + index % 26)
        if index < 8:
            product = '\u201c' + product + '\u201d'
        elif index < 18:
            organisation = 'Zuvr\u00fcn Guild'
        action = 'qualify the ' + product + ' batch ' + tag
        if 30 <= index < 39:
            action = 'reach 40% yield with its ' + product + ' batch ' + tag
        if 6 <= index < 18:
            action += ' between two trial cycles'
        if 20 <= index < 30:
            action += ' (guild test)'
        date = ''
        if index < 49 and index != 5:
            date = ' during ' + MONTHS[index % 12] + ' ' + str(5 + index % 20) + ', 2026'
        elif index < 65:
            date = ' in 2026'
        else:
            date = ' in the Nolvex trial'
        title = 'Will SYNTHETIC ' + organisation + ' ' + action + date + '?'
        if index == 0:
            title = 'Will SYNTHETIC ' + organisation + ' ' + action + date + ' by protocol?'
        elif index == 1:
            title = 'Will SYNTHETIC ' + organisation + ' ' + action + ' before the bell? (February 9, 2026)'
        elif index in (2, 3, 4):
            word = ('nearby', 'standby', 'whereby')[index - 2]
            title = 'Will SYNTHETIC ' + organisation + ' ' + action + ' ' + word + date + '?'
        elif index == 5:
            title = 'Will SYNTHETIC ' + organisation + ' appoint a mayor for its ' + product + ' trial in 2026?'
        elif index == 69:
            title = 'Which SYNTHETIC ' + organisation + ' trial will qualify the ' + product + ' batch ' + tag + '?'
        elif index == 70:
            title = 'On which SYNTHETIC ' + organisation + ' trial will the ' + product + ' batch ' + tag + ' qualify?'
        group = 'spring' if index < 26 else 'summer' if index < 46 else 'minibench'
        other.append({'post':post, 'group':group, 'title':title})
    return {'_label':'SYNTHETIC',
            '_source':'SYNTHETIC. Invented titles and ids made by tests/edge2_titles.py (random.Random(20260928), with its own templates and word lists; it reads no file and no network). No title is, or is derived from, a real question. Regenerate with: python -B tests/edge2_titles.py --write',
            '_written_by':'tests/edge2_titles.py, seed 20260928, 2026-09-28',
            '_rule':RULE, 'regex':PATTERN,
            'counts':{'deadline':88,'deadline_season':61,'deadline_minibench':27,'not_deadline':71},
            'deadline':deadline,'not_deadline':other}


def render(data):
    return json.dumps(data, indent=1, ensure_ascii=True) + '\n'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--write', action='store_true')
    args = parser.parse_args()
    path = Path(__file__).parent / 'fixtures/edge2/deadline_titles.json'
    text = render(build())
    if args.write:
        path.write_text(text, encoding='ascii', newline='\n')
    matches = path.read_bytes() == text.encode('ascii')
    print(('OK' if matches else 'DIFFERS') + ' deadline=88 not_deadline=71 total=159')
    raise SystemExit(0 if matches else 1)


if __name__ == '__main__':
    main()
