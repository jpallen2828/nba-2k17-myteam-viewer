"""Exact odds, boundary sampling, and UI labels for Super Budget."""
from fractions import Fraction
from math import lcm
import random
import pytest
from nba2k17_viewer.teams import (TIERS, DRAFT_KEYS, TeamSession, draft_odds, bonus_odds,
                                percentage_text, roll, bonus_round_name)

@pytest.mark.parametrize('role,raw,targets', [
    ('starter', ['0.0000001','0.00001','0.0001','0.01','1','15','25','25','20','0.001'], range(5,9)),
    ('backup', ['0.000000001','0.0000001','0.00001','0.001','0.1','5','15','45','30','0.00001'], range(6,9)),
    ('bench', ['0.000000000001','0.0000000001','0.00000001','0.000001','0.0001','0.001','1','2','97','0.00000001'], range(7,9)),
])
def test_exact_requested_odds(role, raw, targets):
    original = list(map(Fraction, raw))
    expected = original[:]
    delta = (100 - sum(original)) / len(targets)
    for i in targets:
        expected[i] += delta
    table = draft_odds('super_budget', role)
    assert [t for t,p in table] == list(TIERS)
    assert [p*100 for t,p in table] == expected
    assert sum(p for t,p in table) == 1
    assert all(p > 0 for t,p in table)
    if role == 'bench':
        assert delta < 0

@pytest.mark.parametrize('role', ['starter','backup','bench','bonus'])
def test_every_tier_reachable_at_both_exact_boundaries(role):
    table = bonus_odds('super_budget') if role == 'bonus' else draft_odds('super_budget',role)
    scale = lcm(*(p.denominator for t,p in table))
    class Ticket:
        def __init__(self, value): self.value = value
        def randrange(self, total):
            assert total == scale
            return self.value
    start = 0
    for tier, chance in table:
        width = int(chance * scale)
        assert width >= 1
        assert roll(table, Ticket(start)) == tier
        assert roll(table, Ticket(start + width - 1)) == tier
        start += width
    assert start == scale

def test_bonus_and_exact_tooltip():
    from nba2k17_viewer.team_ui import TeamWorkspace
    table = bonus_odds('super_budget')
    assert [percentage_text(p) for t,p in table] == ['0.001%','0.01%','0.1%','1%','98.889%']
    assert sum(p for t,p in table) == 1
    tip = TeamWorkspace.odds_tooltip('super_budget')
    assert 'Super Budget Draft' in tip and 'Sapphire round' in tip
    assert '0.000000000001%' in tip
    assert '(' not in tip and 'repeating' not in tip
    assert '16.632993299666667%' in tip
    assert 'e-' not in tip.replace('white-space:nowrap;', '')
    for role in ('starter','backup','bench'):
        for tier,p in draft_odds('super_budget',role):
            assert percentage_text(p) in tip
    assert bonus_round_name('budget') == 'Amethyst'
    assert bonus_round_name('default') == 'Diamond'

def test_full_draft_restore_and_independent_bonus_rolls(tmp_path):
    rows = [dict(source_card_id=f'{pos}-{tier}-{i}',name=f'{pos}-{tier}-{i}',position=pos,tier=tier,has_roster_template=True)
            for pos in ('PG','SG','SF','PF','C') for tier in TIERS for i in range(20)]
    s=TeamSession(rows,'draft',random.Random(17));s.mode='super_budget'
    for key in DRAFT_KEYS[:-1]:
        pack=s.open_pack(key)
        assert len(pack)==5
        s.pick(pack[0]['source_card_id'])
    class BonusRolls:
        def __init__(self): self.tickets=iter([0,1,11,111,1111]);self.calls=0
        def randrange(self,total):
            assert total==100000
            self.calls+=1
            return next(self.tickets)
        def choice(self, cards): return cards[0]
    s.rng=BonusRolls()
    pack=s.open_pack('bonus')
    assert [r['tier'] for r in pack]==list(TIERS[:5])
    assert s.rng.calls==5
    target=tmp_path/'draft.2k17team';s.save(target)
    restored=TeamSession.load(rows,target)
    assert restored.mode=='super_budget'
    assert restored.open_pack('bonus')==pack
    restored.pick(pack[0]['source_card_id'])
    assert len(restored.lineup())==13
