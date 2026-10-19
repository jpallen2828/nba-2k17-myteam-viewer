"""Small-team Blacktop drafts; every regular and bonus pick is injectable."""
from .teams import TeamSession, DRAFT_MODES, POSITIONS, player_key, roll, draft_odds, bonus_odds


ROLES = ('guard', 'wing', 'big')
ROLE_LABELS = {'guard': 'Guard', 'wing': 'Wing', 'big': 'Big',
               'guard_wing': 'Guard / Wing', 'wing_big': 'Wing / Big'}


def eligible_role(row, role):
    primary = str(row.get('position') or '').strip().upper()
    secondary = str(row.get('secondary_position') or '').strip().upper()
    roles = {
        'guard': primary in ('PG', 'SG'),
        'wing': primary in ('SG', 'SF') or (primary == 'PF' and secondary != 'C'),
        'big': primary == 'C' or (primary == 'PF' and secondary == 'C'),
    }
    if role == 'guard_wing': return roles['guard'] or roles['wing']
    if role == 'wing_big': return roles['wing'] or roles['big']
    return roles.get(role, False)


class BlacktopDraftSession(TeamSession):
    def __init__(self, rows, rng=None):
        super().__init__(rows, 'draft', rng)
        self.kind = 'blacktop'
        self.size = 1
        self.solo_role = 'guard'
        self.flex_role = 'wing'

    @property
    def regular_keys(self):
        return tuple(f'blacktop|{i + 1}' for i in range(self.size))

    @property
    def keys(self):
        return self.regular_keys + ('bonus',)

    @property
    def complete(self):
        return all(key in self.selected for key in self.keys)

    def configure(self, *, size=None, solo_role=None):
        if self.selected or self.pending:
            raise ValueError('Start a new draft before changing the team format or position group.')
        size = self.size if size is None else size
        solo_role = self.solo_role if solo_role is None else solo_role
        if type(size) is not int or not 1 <= size <= 5 or solo_role not in ROLES:
            raise ValueError('Invalid Blacktop team settings')
        if size == 5 and self.size != 5:
            self.flex_role = self.rng.choice(('wing', 'big'))
        self.size, self.solo_role = size, solo_role

    def roles(self):
        return {1: (self.solo_role,), 2: ('guard_wing', 'wing_big'),
                3: ('guard', 'wing', 'big'), 4: ('guard', 'wing', 'wing', 'big'),
                5: ('guard', 'guard', 'wing', self.flex_role, 'big')}[self.size]

    def slot_label(self, key):
        if key == 'bonus': return 'Bonus player'
        return f'Slot {self.regular_keys.index(key) + 1} · {ROLE_LABELS[self.roles()[self.regular_keys.index(key)]]}'

    def eligible(self, row, key):
        if key == 'bonus': return any(eligible_role(row, role) for role in self.roles())
        return eligible_role(row, self.roles()[self.regular_keys.index(key)])

    def lineup(self):
        return [(key, self.rows[self.selected[key]]) for key in self.keys if key in self.selected]

    def active_lineup(self):
        # Kept for saved-draft callers: every displayed card is active.
        return self.lineup()

    def open_pack(self, key):
        if key not in self.keys or key in self.selected:
            raise ValueError('This Blacktop draft slot is unavailable')
        if self.pending:
            if key != self.pending: raise ValueError('Finish your current pack first')
            return [self.rows[cid] for cid in self.options]
        if key == 'bonus' and not all(k in self.selected for k in self.regular_keys):
            raise ValueError('Choose all regular Blacktop players before the bonus round')
        used = {player_key(self.rows[cid]) for cid in self.selected.values()}
        table = bonus_odds(self.mode) if key == 'bonus' else draft_odds(self.mode, 'starter')
        allowed = {tier for tier, chance in table if chance > 0}
        choices = []
        for _ in range(5):
            tier = roll(table, self.rng)
            eligible = [r for r in self.pool() if r['position'] in POSITIONS
                        and self.eligible(r, key) and player_key(r) not in used]
            pool = [r for r in eligible if r['tier'] == tier]
            if not pool: pool = [r for r in eligible if r['tier'] in allowed]
            if not pool: break
            card = self.rng.choice(pool)
            choices.append(str(card['source_card_id']))
            used.add(player_key(card))
        if not choices: raise ValueError('No eligible cards remain for this Blacktop pack')
        self.pending, self.options = key, choices
        return [self.rows[cid] for cid in choices]

    def data(self):
        return dict(format='nba2k17-blacktop/v1', kind='blacktop', mode=self.mode,
                    include_custom=self.include_custom, size=self.size, solo_role=self.solo_role,
                    flex_role=self.flex_role, selected=dict(self.selected), pending=self.pending,
                    options=list(self.options))

    @classmethod
    def from_data(cls, rows, data):
        if not isinstance(data, dict) or data.get('format') != 'nba2k17-blacktop/v1' or data.get('kind') != 'blacktop':
            raise ValueError('Not a saved Blacktop draft')
        obj = cls(rows)
        if type(data.get('size')) is not int or data.get('solo_role') not in ROLES:
            raise ValueError('Invalid Blacktop team settings')
        obj.configure(size=data.get('size'), solo_role=data.get('solo_role'))
        obj.mode = data.get('mode')
        obj.include_custom = data.get('include_custom')
        obj.flex_role = data.get('flex_role')
        if obj.mode not in DRAFT_MODES or type(obj.include_custom) is not bool or obj.flex_role not in ('wing', 'big'):
            raise ValueError('Invalid Blacktop draft settings')
        obj.selected, obj.pending, obj.options = data.get('selected'), data.get('pending'), data.get('options')
        if not isinstance(obj.selected, dict) or not isinstance(obj.options, list):
            raise ValueError('Invalid Blacktop draft picks')
        ids = list(obj.selected.values()) + obj.options
        allowed = {cid for cid, row in obj.rows.items() if row.get('has_roster_template', False)}
        if any(not isinstance(cid, str) or cid not in allowed for cid in ids):
            raise ValueError('This Blacktop draft references unavailable or ineligible cards')
        if any(key not in obj.keys for key in obj.selected) or len(obj.options) > 5 or len(set(obj.options)) != len(obj.options):
            raise ValueError('Invalid Blacktop slots or pack size')
        if any(not obj.eligible(obj.rows[cid], key) for key, cid in obj.selected.items()):
            raise ValueError('A saved Blacktop player does not fit its slot')
        if 'bonus' in obj.selected and not all(k in obj.selected for k in obj.regular_keys):
            raise ValueError('Blacktop bonus requires all regular picks')
        if obj.pending is not None and not isinstance(obj.pending, str):
            raise ValueError('Invalid pending Blacktop pack')
        if bool(obj.pending) != bool(obj.options) or (obj.pending and
                (obj.pending not in obj.keys or obj.pending in obj.selected)):
            raise ValueError('Invalid pending Blacktop pack')
        if obj.pending:
            if obj.pending == 'bonus' and not all(k in obj.selected for k in obj.regular_keys):
                raise ValueError('Blacktop bonus requires all regular picks')
            allowed_tiers = {t for t, chance in (bonus_odds(obj.mode) if obj.pending == 'bonus' else draft_odds(obj.mode, 'starter')) if chance > 0}
            if any(not obj.eligible(obj.rows[cid], obj.pending) or obj.rows[cid]['tier'] not in allowed_tiers for cid in obj.options):
                raise ValueError('A saved Blacktop pack contains an ineligible choice')
            if not obj.include_custom and any(cid.startswith('custom-') for cid in obj.options):
                raise ValueError('Custom cards are disabled for this pending pack')
        players = [player_key(obj.rows[cid]) for cid in ids]
        if len(players) != len(set(players)):
            raise ValueError('Players cannot repeat in a Blacktop draft or pending pack')
        # Older saves may contain bonus_slot. All original picks are retained;
        # the obsolete substitution selection has no effect on injection.
        return obj
