"""Apply user-approved catalog curation to future exports, not raw evidence."""
from nba2k17_catalog import exclusion_reason, jersey_evidence
from nba2k17_catalog.badge_estimates import apply_estimate, load_overlay
from nba2k17_catalog.personality import harmonize_personalities
from .models import DataSection, ValueStatus


def curate_catalog(cards):
    result = []
    badge_estimates = load_overlay()
    for card in cards:
        site, card_id = card.source.get('primary_site'), card.source.get('source_card_id')
        if exclusion_reason(site, card_id):
            continue
        curated, estimate = apply_estimate(card.to_dict(), badge_estimates)
        if estimate:
            card.badges = DataSection.from_dict(curated['badges'])
            card.raw_source['badge_estimation'] = estimate
        evidence = jersey_evidence(site, card_id)
        if evidence:
            card.identity.set('jersey_number', evidence['number'], status=ValueStatus.WEB_CARD_SPECIFIC,
                              raw_label='Printed bottom-left card jersey number')
            provenance = {'source_site': '2kmtcentral', 'source_card_id': str(card_id),
                          'snapshot_sha256': evidence['art_sha256'],
                          'source_label': 'Printed bottom-left card jersey number',
                          'note': 'Offline per-card PNG glyph transcription; every distinct glyph shape visually reviewed. 0 and 00 remain distinct.'}
            if provenance not in card.identity.provenance:
                card.identity.provenance.append(provenance)
        result.append(card)
    projections, _ = harmonize_personalities(card.to_dict() for card in result)
    for card, projection in zip(result, projections):
        card.badges = DataSection.from_dict(projection['badges'])
        if 'personality_curation' in projection.get('raw_source', {}):
            card.raw_source['personality_curation'] = projection['raw_source']['personality_curation']
    return result
