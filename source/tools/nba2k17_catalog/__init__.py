"""Shared local card curation. No network, game access, or database writes."""
from functools import lru_cache
import json
from pathlib import Path


TIERS = ("Pink Diamond", "Diamond", "Amethyst", "Ruby", "Sapphire", "Emerald", "Gold", "Silver", "Bronze", "Onyx")
TIER_RANK = {tier: rank for rank, tier in enumerate(TIERS)}
DEFAULT_SORT = "Tier first · highest OVR"
LOW_OVR_SORT = "Tier first · lowest OVR"

# User-directed removals, 2026-09-07. Source-scoped IDs, never player-name
# exclusions: the distinct 90 OVR Free Agents 2 LeBron (8009) is retained.
EXCLUDED_2KMTC_CARDS = {
    "8172": "User removed 77 OVR Gold Kenny Gattison, 2026-09-10.",
    "2796": "User removed 64 OVR Pink Diamond Ricky Ledo, 2026-09-09.",
    "1844": "User removed Bronze Steve Brown, 2026-09-09.",
    "4847": "User removed Bronze John Smith, 2026-09-09.",
    "4862": "User removed Bronze John Smith, 2026-09-09.",
    "4868": "User removed Bronze John Smith, 2026-09-09.",
    "4905": "User removed Bronze John Smith, 2026-09-09.",
    "8096": "User removed unparsed card #8096, 2026-09-08.",
    "2128": "User removed the 70 OVR 1972 Keith Erickson; user identified its franchise as Lakers. Retain 1971 card 2113.",
    "2230": "User removed the 73 OVR Silver Hubert Davis, 2026-09-08.",
    "2184": "User removed the 68 OVR Bronze Elden Campbell, 2026-09-08.",
    "8004": "User removed the missing-PNG 90 OVR 2015 Free Agents LeBron James.",
    "32000": "User removed the 2KMTCentral 2nd Birthday promotional card.",
}

# User-approved PNG-less duplicates. Raw archives remain intact. All 48
# User-directed fictional Bronze removals, 2026-09-08. Exact source IDs
# include name variants with website rating/title suffixes; never name-match
# unrelated real players or exclude an entire collection.
FICTIONAL_BRONZE_IDS = frozenset('''
1814 1815 1817 1824 1828 1831 1832 1835 1836 1837 1838 1843 1845 1846
1850 1851 1855 1856 1857 1860 1861 1862 1864 1868 1869 1871 1872 1874
1875 1876 1882 1884 1885 1886 1889 1890 1891 1892 1894 1895 1900 1901
1902 1903 1905 1906 1909 1911 1912 1913 2404 2405 2516 2524 2526 2533
2535 2538 2539 4204 4205 4843 4845 4849 4850 4852 4854 4857 4858 4860
4861 4863 4864 4865 4867 4870 4871 4872 4873 4874 4877 4878 4879 4880
4885 4889 4893 4896 4899 4900 4901 4904 4907 4908 4911
'''.split())
EXCLUDED_2KMTC_CARDS.update({cid: 'User permanently removed fictional Bronze roster filler.'
                              for cid in FICTIONAL_BRONZE_IDS})

# User-approved PNG-less duplicates. Raw archives remain intact. All 48
# attributes, 14 hot zones and declared badge counts match the retained ID;
# no unique verified values were found. NO SUBCOLLECTION is a placeholder,
# not a distinct collection. See pngless_duplicate_audit_2026-09-08.json.
PNGLESS_DUPLICATES = {
    # Same player/year/tier/OVR and identical available gameplay values. The
    # visible franchise cards retain all information (Sam also has tendencies).
    '2089': '8134', '2090': '8139',
    '20462': '20213', '20463': '20214', '20464': '20215',
    '20465': '20216', '20466': '20217', '20467': '20218',
    '20468': '20219', '20469': '20220', '20470': '20222',
    '20471': '20223', '20472': '20224', '20473': '20225',
    '20474': '20226', '20475': '20227',
}
EXCLUDED_2KMTC_CARDS.update({
    removed: f'User removed PNG-less duplicate; retained 2KMTC card {kept}.'
    for removed, kept in PNGLESS_DUPLICATES.items()
})


def exclusion_reason(source_site, source_card_id):
    return EXCLUDED_2KMTC_CARDS.get(str(source_card_id)) if str(source_site) == '2kmtcentral' else None


def tier_rank(tier):
    return TIER_RANK.get(tier, len(TIERS))


@lru_cache(maxsize=1)
def jersey_transcriptions():
    return json.loads(Path(__file__).with_name('jersey_numbers.json').read_text(encoding='utf-8'))['cards']


def jersey_evidence(source_site, source_card_id):
    if str(source_site) != '2kmtcentral' or exclusion_reason(source_site, source_card_id):
        return None
    entry = jersey_transcriptions().get(str(source_card_id))
    return dict(entry) if entry else None
