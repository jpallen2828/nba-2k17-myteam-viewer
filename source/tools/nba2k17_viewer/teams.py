"""Offline lineup rules ported from the 2K16 viewer, using 2K17 card IDs."""
import json
import random
import re
import unicodedata
from pathlib import Path
from fractions import Fraction
from decimal import Decimal, localcontext
from math import lcm

POSITIONS = ('PG', 'SG', 'SF', 'PF', 'C')
TIERS = ('Pink Diamond', 'Diamond', 'Amethyst', 'Ruby', 'Sapphire', 'Emerald', 'Gold', 'Silver', 'Bronze', 'Onyx')
BASE_TIERS = ('Pink Diamond', 'Diamond', 'Amethyst', 'Gold', 'Silver', 'Bronze')
ODDS = {
    'baller': {'starter': (.10, .20, .30, .20, .199, .001), 'backup': (.05, .05, .15, .60, .10, .05), 'bench': (.005, .01, .085, .40, .40, .10)},
    'default': {'starter': (.01675, .05, .18325, .40, .30, .05), 'backup': (.001, .0025, .0365, .26, .50, .20), 'bench': (.0000335, .0001, .0009665, .01, .40, .5889)},
    'budget': {'starter': (.001, .002, .097, .40, .40, .10), 'backup': (.0005, .001, .0185, .15, .60, .23), 'bench': (.000005, .00001, .000095, .001, .15, .84889)},
}
POSITION_KEYS = tuple(f'{role}|{pos}' for pos in POSITIONS for role in ('starter', 'backup'))
DRAFT_MODES = (*ODDS, 'super_budget')
# Exact percentages keep even the rarest draw reachable and preserve equal splits.
SUPER_BUDGET_PERCENTAGES = {
    'starter': ('0.0000001', '0.00001', '0.0001', '0.01', '1', '15', '25', '25', '20', '0.001'),
    'backup': ('0.000000001', '0.0000001', '0.00001', '0.001', '0.1', '5', '15', '45', '30', '0.00001'),
    'bench': ('0.000000000001', '0.0000000001', '0.00000001', '0.000001', '0.0001', '0.001', '1', '2', '97', '0.00000001'),
}


def super_budget_odds(role):
    values = dict(zip(TIERS, map(Fraction, SUPER_BUDGET_PERCENTAGES[role])))
    targets = {'starter': TIERS[5:9], 'backup': TIERS[6:9], 'bench': TIERS[7:9]}[role]
    adjustment = (100 - sum(values.values())) / len(targets)
    for tier in targets:
        values[tier] += adjustment
    return [(tier, value / 100) for tier, value in values.items()]


def percentage_text(chance):
    """Plain decimal percentages; recurring values display to 15 decimal places."""
    if not isinstance(chance, Fraction):
        return f'{chance * 100:.5g}%'
    with localcontext() as context:
        context.prec = 50
        value = Decimal(chance.numerator) * 100 / Decimal(chance.denominator)
        return format(value, '.15f').rstrip('0').rstrip('.') + '%'


def bonus_round_name(mode):
    return {'budget': 'Amethyst', 'super_budget': 'Sapphire'}.get(mode, 'Diamond')
DRAFT_KEYS = POSITION_KEYS + ('bench|1', 'bench|2', 'bonus')
LINEUP_KEYS = tuple(f'{role}|{pos}' for role in ('starter', 'backup') for pos in POSITIONS) + DRAFT_KEYS[10:]

def player_key(row):
    name = re.sub(r'\s*\(\d+\).*$', '', row['name'])
    return ''.join(c for c in unicodedata.normalize('NFKD', name).casefold() if c.isalnum())

def draft_odds(mode, role):
    if mode == 'super_budget':
        return super_budget_odds(role)
    result = {}
    for tier, chance in zip(BASE_TIERS, ODDS[mode][role]):
        targets = ('Ruby', 'Sapphire', 'Emerald', 'Gold') if tier == 'Gold' else ('Bronze', 'Onyx') if tier == 'Bronze' else (tier,)
        result.update((t, chance / len(targets)) for t in targets)

    def transfer(source, amount, targets):
        result[source] -= amount
        for target in targets:
            result[target] += amount / len(targets)

    if mode == 'baller':
        if role == 'starter':
            result = dict(zip(TIERS, (.225, .225, .20, .05, .05, .05, .05, .05, .05, .05)))
        elif role == 'backup':
            transfer('Ruby', .05, ('Diamond',))
            transfer('Amethyst', .05, ('Pink Diamond',))
        elif role == 'bench':
            transfer('Silver', .20, ('Gold', 'Emerald'))
    elif mode == 'default':
        if role == 'starter':
            transfer('Amethyst', .01675, ('Pink Diamond',))
            transfer('Silver', .20, ('Gold', 'Emerald', 'Sapphire'))
        elif role == 'backup':
            transfer('Silver', .40, ('Gold', 'Emerald', 'Sapphire', 'Ruby'))
        elif role == 'bench':
            transfer('Onyx', result['Onyx'], ('Bronze', 'Silver', 'Gold'))
    elif mode == 'budget':
        if role == 'starter':
            transfer('Silver', .20, ('Gold', 'Emerald'))
        elif role == 'backup':
            starters = dict(draft_odds('budget', 'starter'))
            result['Onyx'] = .005
            for tier in TIERS[:5]:
                result[tier] = starters[tier] / 2
            remainder = 1 - sum(result[tier] for tier in TIERS[:5]) - result['Onyx']
            for tier in TIERS[5:-1]:
                result[tier] = remainder / 4
        elif role == 'bench':
            transfer('Onyx', result['Onyx'] - .0001, ('Bronze', 'Silver'))
    return list(result.items())

def bonus_odds(mode):
    if mode == 'super_budget':
        return list(zip(TIERS[:5], (Fraction(value) / 100 for value in ('0.001', '0.01', '0.1', '1', '98.889'))))
    if mode == 'budget':
        return [('Pink Diamond', .001), ('Diamond', .019), ('Amethyst', .98)]
    pink = .50 if mode == 'baller' else .10
    return [('Pink Diamond', pink), ('Diamond', 1 - pink)]

def roll(table, rng):
    if all(isinstance(chance, Fraction) for _, chance in table):
        denominator = lcm(*(chance.denominator for _, chance in table))
        weights = [(tier, chance.numerator * (denominator // chance.denominator)) for tier, chance in table]
        ticket = rng.randrange(sum(weight for _, weight in weights))
        for tier, weight in weights:
            if ticket < weight:
                return tier
            ticket -= weight
        raise AssertionError('Draw outside probability table')
    value = rng.random()
    for tier, chance in table:
        value -= chance
        if value < 0:
            return tier
    return next(tier for tier, chance in reversed(table) if chance > 0)

class TeamSession:
    def __init__(self, rows, kind, rng=None):
        if kind not in ('custom', 'random', 'draft'):
            raise ValueError('Unknown team mode')
        self.rows = {str(r['source_card_id']): r for r in rows}
        self.kind, self.rng = kind, rng or random.Random()
        self.mode = 'default'
        self.include_custom = True
        self.selected = {}
        self.custom = []
        self.pending = None
        self.options = []

    def pool(self):
        return [r for cid, r in self.rows.items()
                if (self.include_custom or not cid.startswith('custom-'))
                and (self.kind == 'custom' or r.get('has_roster_template', False))]

    def lineup(self):
        if self.kind == 'custom':
            return [(str(i + 1), self.rows[cid]) for i, cid in enumerate(self.custom)]
        keys = POSITIONS if self.kind == 'random' else LINEUP_KEYS
        return [(key, self.rows[self.selected[key]]) for key in keys if key in self.selected]

    def add(self, cid):
        if self.kind != 'custom' or len(self.custom) >= 13 or cid not in {str(r['source_card_id']) for r in self.pool()}:
            raise ValueError('Cannot add this card; custom teams allow up to 13 cards')
        self.custom.append(cid)

    def generate_random(self):
        chosen, used = {}, set()
        for pos in POSITIONS:
            pool = [r for r in self.pool() if r['position'] == pos and player_key(r) not in used]
            tiers = [tier for tier in TIERS if any(r['tier'] == tier for r in pool)]
            if not tiers:
                raise ValueError(f'No eligible {pos} card in this archive')
            tier = self.rng.choice(tiers)
            card = self.rng.choice([r for r in pool if r['tier'] == tier])
            chosen[pos] = str(card['source_card_id'])
            used.add(player_key(card))
        self.selected = chosen

    def open_pack(self, key):
        if self.kind != 'draft' or key not in DRAFT_KEYS or key in self.selected:
            raise ValueError('This draft slot is unavailable')
        if self.pending:
            if key != self.pending:
                raise ValueError('Finish your current pack first')
            return [self.rows[cid] for cid in self.options]
        if key.startswith('bench') and not all(k in self.selected for k in POSITION_KEYS):
            raise ValueError('Choose all starters and backups before the deep bench')
        if key == 'bonus' and not all(k in self.selected for k in DRAFT_KEYS[:-1]):
            raise ValueError('Choose the first 12 players before the bonus round')
        used = {player_key(self.rows[cid]) for cid in self.selected.values()}
        choices = []
        for index in range(5):
            if key == 'bonus':
                table = bonus_odds(self.mode)
                tier = roll(table, self.rng)
                allowed = {t for t, _ in table}
                pos = None
            else:
                role, pos = key.split('|')
                table = draft_odds(self.mode, role)
                tier = roll(table, self.rng)
                pos = None if role == 'bench' else pos
                allowed = {t for t, chance in table if chance > 0}
            eligible = [r for r in self.pool() if r['position'] in POSITIONS and (pos is None or r['position'] == pos) and player_key(r) not in used]
            pool = [r for r in eligible if r['tier'] == tier]
            if not pool:
                pool = [r for r in eligible if r['tier'] in allowed]
            if not pool:
                break
            card = self.rng.choice(pool)
            choices.append(str(card['source_card_id']))
            used.add(player_key(card))
        if not choices:
            raise ValueError('No eligible cards remain for this pack')
        self.pending, self.options = key, choices
        return [self.rows[cid] for cid in choices]

    def pick(self, cid):
        if not self.pending or cid not in self.options:
            raise ValueError('Choose a card from the current pack')
        self.selected[self.pending] = cid
        self.pending, self.options = None, []

    def data(self):
        return dict(format='nba2k17-team/v1', kind=self.kind, mode=self.mode, include_custom=self.include_custom,
                    selected=self.selected, custom=self.custom, pending=self.pending, options=self.options)

    @classmethod
    def from_data(cls, rows, data):
        if not isinstance(data, dict) or data.get('format') != 'nba2k17-team/v1':
            raise ValueError('Not a 2K17 saved team')
        obj = cls(rows, data.get('kind'))
        obj.mode = data.get('mode', 'default')
        obj.include_custom = data.get('include_custom', True)
        if obj.mode not in DRAFT_MODES or type(obj.include_custom) is not bool:
            raise ValueError('Invalid draft settings')
        obj.selected, obj.custom = data.get('selected', {}), data.get('custom', [])
        obj.pending, obj.options = data.get('pending'), data.get('options', [])
        if not isinstance(obj.selected, dict) or not isinstance(obj.custom, list) or not isinstance(obj.options, list):
            raise ValueError('Invalid lineup data')
        ids = list(obj.selected.values()) + obj.custom + obj.options
        if any(not isinstance(cid, str) or cid not in obj.rows for cid in ids):
            raise ValueError('This team references cards missing from the current archive')
        if len(obj.custom) > 13 or len(obj.options) > 5 or len(set(obj.options)) != len(obj.options):
            raise ValueError('Invalid lineup size')
        if obj.kind == 'custom':
            if obj.selected or obj.pending or obj.options:
                raise ValueError('Invalid custom team')
        else:
            keys = POSITIONS if obj.kind == 'random' else DRAFT_KEYS
            if any(not obj.rows[cid].get('has_roster_template', False) for cid in ids):
                raise ValueError('This saved draft/random team contains cards without a roster template. They remain available in Create a Team.')
            if obj.custom or any(k not in keys for k in obj.selected):
                raise ValueError('Invalid team slots')
            players = [player_key(obj.rows[cid]) for cid in obj.selected.values()]
            if len(players) != len(set(players)):
                raise ValueError('Duplicate players in a random or drafted team')
            for key, cid in obj.selected.items():
                pos = key if obj.kind == 'random' else key.split('|')[-1]
                if pos in POSITIONS and obj.rows[cid]['position'] != pos:
                    raise ValueError('A player is in the wrong position')
            if obj.kind == 'random' and (obj.pending or obj.options):
                raise ValueError('Random teams cannot contain draft packs')
            if obj.kind == 'draft':
                if any(k.startswith('bench') or k == 'bonus' for k in obj.selected) and not all(k in obj.selected for k in POSITION_KEYS):
                    raise ValueError('Bench requires ten position picks')
                if 'bonus' in obj.selected and not all(k in obj.selected for k in DRAFT_KEYS[:-1]):
                    raise ValueError('Bonus requires twelve picks')
                if bool(obj.pending) != bool(obj.options) or (obj.pending and (obj.pending not in DRAFT_KEYS or obj.pending in obj.selected)):
                    raise ValueError('Invalid pending draft pack')
                if obj.pending:
                    if obj.pending.startswith('bench') and not all(k in obj.selected for k in POSITION_KEYS):
                        raise ValueError('Pending bench pack requires ten position picks')
                    if obj.pending == 'bonus' and not all(k in obj.selected for k in DRAFT_KEYS[:-1]):
                        raise ValueError('Pending bonus pack requires twelve picks')
                    pos = obj.pending.split('|')[-1]
                    if any(obj.rows[cid]['position'] not in POSITIONS or (pos in POSITIONS and obj.rows[cid]['position'] != pos) for cid in obj.options):
                        raise ValueError('Ineligible player in pending draft pack')
                offered = [player_key(obj.rows[cid]) for cid in obj.options]
                if len(set(offered)) != len(offered) or set(offered) & set(players):
                    raise ValueError('Duplicate players in draft choices')
        return obj

    def save(self, path):
        target = Path(path)
        temp = target.with_name(target.name + '.tmp')
        temp.write_text(json.dumps(self.data(), indent=2), encoding='utf-8')
        temp.replace(target)

    @classmethod
    def load(cls, rows, path):
        path = Path(path)
        if path.stat().st_size > 1_000_000:
            raise ValueError('Saved team is too large')
        return cls.from_data(rows, json.loads(path.read_text(encoding='utf-8')))
