"""Match real roster slots to cards without changing their archived years."""
from .face_ids import normalized_name

ALIASES={'nen':'nene','mingyao':'yaoming','pennyhardaway':'anferneehardaway',
         'worldbfree':'worldfree','natemcmillan':'natemcmillian','davidwingate':'davewingate',
         'louiswilliams':'louwilliams','cliffrobinson':'cliffordrobinson',
         'mettaworldpeace':'ronartest','michaelrayrichardson':'michealrayrichardson',
         'gheorghemuresan':'gheorgemuresan'}
FRANCHISES={'seattlesupersonics':'oklahomacitythunder','newjerseynets':'brooklynnets',
            'vancouvergrizzlies':'memphisgrizzlies','charlottebobcats':'charlottehornets',
            'neworleanshornets':'neworleanspelicans'}


def player_key(name):
    # Keep Jr./Sr. and generation suffixes: Tim Hardaway is not his son.
    key=normalized_name(name or '')
    return ALIASES.get(key,key)


def franchise_key(name):
    key=normalized_name(name or '')
    return FRANCHISES.get(key,key)


def year_hint(identity,audit=None):
    year=identity.get('card_year')
    if type(year) is int:return year,'CURRENT_CARD_YEAR'
    original=(audit or {}).get('expected',{}).get('card_year')
    if type(original) is int:return original,'ORIGINAL_LISTED_YEAR_HINT_CARD_YEAR_REMAINS_UNRESOLVED'
    return None,'NO_LISTED_YEAR'


def select_source(identity,candidates,audit=None):
    """Candidates already have independently verified live team references."""
    year,origin=year_hint(identity,audit)
    if not candidates:return None,{'reason':'NO_VERIFIED_TEAM_SLOT','year_hint':year,'year_hint_origin':origin}
    same=lambda c:bool(identity.get('team')) and franchise_key(identity['team'])==franchise_key(c['context']['team'])
    dated=[c for c in candidates if type(c['context'].get('season')) is int]
    if year is not None and dated:
        score=lambda c:(abs(year-c['context']['season']),not same(c),c['context']['season'])
        rank=min(map(score,dated));best=[c for c in dated if score(c)==rank]
        method='CLOSEST_AVAILABLE_YEAR_FRANCHISE_TIEBREAK_EARLIER_EQUAL_DISTANCE'
    else:
        best=[c for c in candidates if same(c)] or candidates
        method='UNIQUE_ROSTER_VARIANT_YEAR_UNRESOLVED'
    # Duplicate references to one record do not create competing variants.
    variants={c['template_id'] for c in best}
    if len(variants)>1:
        return None,{'reason':'EQUALLY_RANKED_DIFFERENT_ROSTER_VARIANTS','year_hint':year,
                     'year_hint_origin':origin,'candidate_template_ids':sorted(variants)}
    choice=sorted(best,key=lambda c:c['team_index'])[0]
    season=choice['context'].get('season')
    return choice,{'method':method,'year_hint':year,'year_hint_origin':origin,
                   'year_distance':abs(year-season) if year is not None and type(season) is int else None,
                   'same_franchise':same(choice),'card_year_modified':False,
                   'exact_card_era_verified':origin=='CURRENT_CARD_YEAR' and year==season and same(choice)}


def team_candidates(teams,name):
    """Prefer dated NBA/national contexts; retain unique other real team slots."""
    known=[];other=[]
    for team in teams:
        for slot in team['slots']:
            if player_key(slot['name'])!=player_key(name):continue
            context=team['context'] or {'team':team['token'],'season':None,
                'roster_kind':'OTHER_LIVE_TEAM','context_status':'SEASON_UNVERIFIED'}
            candidate={**slot,'team_index':team['team_index'],'context':context}
            (known if team['context'] else other).append(candidate)
    return known or other
