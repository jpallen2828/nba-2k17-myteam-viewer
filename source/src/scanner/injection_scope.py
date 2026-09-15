"""User-approved current/historic NBA scope, with East/West isolation deferred."""

DEFERRED_TEAMS = frozenset({'eastern_conference_logo', 'western_conference_logo'})
POLICY = 'CURRENT_AND_HISTORIC_NBA_DEFER_EAST_WEST_ISOLATION'


def partition_references(references, *, defer_all_stars=False):
    """Keep every alias visible; only the two identified conference teams defer.

    Unknown/unnamed teams stay protected. Deferring isolation neither enables
    writes nor proves the ownership of referenced body/appearance storage.
    """
    protected, deferred = [], []
    for ref in references:
        token = ref.get('team_token', ref.get('team', ref.get('team_identity_token')))
        (deferred if defer_all_stars and token in DEFERRED_TEAMS else protected).append(ref)
    return protected, deferred
