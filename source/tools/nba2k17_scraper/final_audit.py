"""Offline evidence audit only. No network calls, crawl-DB writes, or app exports."""
from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

from .asset_validator import validate_image_bytes
from .normalizer import normalize_parsed_card, canonical_key, ATTRIBUTE_ALIASES, TENDENCY_ALIASES
from .sources import SourcePage, TwoKMTCentralAdapter

ROOT = Path('.project/data/nba2k17').resolve()
OUT = ROOT / 'reports/final_audit_2026-09-07'
TIERS = ('BRONZE', 'SILVER', 'GOLD', 'HALL_OF_FAME')
SKIP = 'source_detail_records'


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def progress(phase, **details):
    value = dict(phase=phase, updated_at=time.time(), **details)
    save(OUT/'audit_progress.json', value)
    print(json.dumps(value), flush=True)


def page_kind(url):
    for kind in ('badges', 'tendencies', 'attributes'):
        if url.rstrip('/').endswith('/'+kind):
            return kind
    return 'main'


def verify(row):
    path = Path(row['raw_cache_path'])
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != row['content_hash']:
        raise ValueError('SHA256 mismatch')
    return body


def parse_card(job):
    card_id, snapshots = job
    adapter = TwoKMTCentralAdapter()
    raw = None
    result = {'source_card_id':card_id, 'pages':[], 'errors':[], 'observations':[]}
    for row in snapshots:
        try:
            candidates=[row, *row.pop('alternates', [])]
            body=None
            for candidate in candidates:
                try:
                    body=verify(candidate)
                    row=candidate
                    break
                except Exception as error:
                    result['errors'].append({'url':candidate['url'],'snapshot_id':candidate['snapshot_id'],
                        'path':candidate['raw_cache_path'],'error':'INTEGRITY: '+str(error)})
            if body is None:
                continue
            parsed = adapter.parse_page(SourcePage(row['url'], body.decode('utf-8',errors='replace'),
                content_hash=row['content_hash'], provenance='WAYBACK_'+str(row['archive_timestamp'])))
            if parsed.source_card_id != card_id:
                raise ValueError('Parsed card ID differs from crawl card ID')
            evidence = dict(row)
            evidence['kind'] = page_kind(row['url'])
            evidence['badge_counts'] = parsed.badge_counts
            result['pages'].append(evidence)
            result['observations'].append({'url':row['url'], 'snapshot_id':row['snapshot_id'],
                'attributes':[x.__dict__ if hasattr(x,'__dict__') else {'label':x.label,'value':x.value,'duo_boost':x.dynamic_duo_boost} for x in parsed.attributes],
                'tendencies':[{'label':x.label,'value':x.value} for x in parsed.tendencies],
                'badges':[{'name':x.name,'level':x.level} for x in parsed.badges]})
            raw = parsed if raw is None else raw.merge(parsed)
        except Exception as error:
            result['errors'].append({'url':row['url'],'snapshot_id':row['snapshot_id'],
                                     'path':row['raw_cache_path'],'error':str(error)})
    if raw is not None:
        result['raw'] = raw.to_dict()
        result['card'] = normalize_parsed_card(raw.to_dict()).to_dict()
    return result


def values(record, domain):
    section = record.get('card',{}).get(domain,{})
    return {k:v for k,v in section.get('values',{}).items()
            if v is not None and k != SKIP and not k.startswith('source_count_')}


def conflicts(record, domain):
    observed = defaultdict(set)
    for p in record['observations']:
        for field in p[domain]:
            if domain == 'badges':
                key = canonical_key(field['name']);value=field['level']
            else:
                label = field['label']
                if domain == 'attributes' and field.get('duo_boost') is not None:
                    label = re.sub(r'\s*[+\-]\s*\d+\s*$','',label)
                key = canonical_key(label)
                aliases = ATTRIBUTE_ALIASES if domain == 'attributes' else TENDENCY_ALIASES
                key = aliases.get(key,key);value=field['value']
            if value is not None:
                observed[key].add(str(value))
    return {k:sorted(v) for k,v in observed.items() if len(v)>1}


def cancellation():
    cp = json.loads((ROOT/'recovery_checkpoint.json').read_text())
    done = set(cp['done'])
    assets = json.loads((ROOT/'known_2kmtcentral_asset_manifest.json').read_text())['assets']
    by_id = defaultdict(list)
    for url in (ROOT/'known_2kmtcentral_subpage_urls.txt').read_text().split():
        by_id[url.split('/players/')[1].split('/')[0]].append(url)
    jobs=[]
    for asset in assets:
        jobs.append('png:'+asset['source_url'])
        jobs.extend('detail:'+url for url in by_id.pop(str(asset['source_card_id']),[]))
    jobs.extend('detail:'+url for group in by_id.values() for url in group)
    attempts=[]
    for line in (ROOT.parent.parent/'logs/nba2k17_recovery.stdout.log').read_text().splitlines():
        try:
            event=json.loads(line)
            if event.get('phase') in ('recovering_png','recovering_detail'):
                attempts.append(event['phase'].removeprefix('recovering_')+':'+event['url'])
        except ValueError:
            pass
    tail=jobs[jobs.index(attempts[-1])+1:] if attempts else []
    result={'decision':'CANCELLED_FINAL_TAIL_NO_MORE_2KMTCENTRAL_RETRIES',
            'checkpoint_resolved':len(done),'total_jobs':len(jobs),
            'cancelled_unattempted_tail':[k for k in tail if k not in done],
            'all_unresolved':[k for k in jobs if k not in done],
            'earlier_unresolved':[k for k in jobs if k not in done and k not in tail]}
    save(OUT/'cancelled_jobs.json',result)
    return result


def run():
    OUT.mkdir(parents=True,exist_ok=True)
    progress('inventory')
    cancelled = cancellation()
    connection = sqlite3.connect((ROOT/'crawl.sqlite').as_uri()+'?mode=ro',uri=True)
    connection.row_factory = sqlite3.Row
    app = ROOT/'normalized/cards.sqlite'
    app_before = {'size':app.stat().st_size,'mtime_ns':app.stat().st_mtime_ns}
    known = set((ROOT/'known_2kmtcentral_urls.txt').read_text().split())
    urls = [dict(r) for r in connection.execute("SELECT crawl_url_id,url,source_card_id,fetch_status,last_error FROM crawl_urls WHERE source='2kmtcentral'")]
    main = [r for r in urls if r['url'] in known]
    groups=defaultdict(list)
    for row in main:
        groups[row['source_card_id']].append(row)
    selected=defaultdict(list)
    all_success=connection.execute("""SELECT u.url,u.source_card_id,s.snapshot_id,s.captured_at,s.archive_timestamp,
        s.content_hash,s.raw_cache_path,s.source_kind,s.http_status
        FROM crawl_urls u JOIN page_snapshots s USING(crawl_url_id)
        WHERE u.source='2kmtcentral' AND s.http_status BETWEEN 200 AND 299
        ORDER BY u.url,COALESCE(s.archive_timestamp,'') DESC,s.snapshot_id DESC""").fetchall()
    chosen={};hash_errors=[]
    for row in all_success:
        row=dict(row)
        if row['source_card_id'] not in groups:
            continue
        if row['url'] in chosen:
            chosen[row['url']].setdefault('alternates',[]).append(row)
            continue
        selected[row['source_card_id']].append(row);chosen[row['url']]=row
    jobs=[]
    for card_id in groups:
        pages=selected[card_id]
        pages.sort(key=lambda p:({'main':0,'attributes':1,'badges':2,'tendencies':3}[page_kind(p['url'])],p['url']))
        jobs.append((card_id,pages))
    records=[]
    progress('parsing_saved_cards',completed=0,total=len(jobs))
    with ProcessPoolExecutor(max_workers=4) as pool:
        futures=[pool.submit(parse_card,job) for job in jobs]
        for future in as_completed(futures):
            records.append(future.result())
            if len(records)%100==0:
                progress('parsing_saved_cards',completed=len(records),total=len(jobs))
    records.sort(key=lambda r:int(r['source_card_id']))
    save(OUT/'parsed_evidence.json',records)
    hash_errors=[e for r in records for e in r['errors'] if e['error'].startswith('INTEGRITY:')]
    progress('validating_saved_pngs',cards=len(records))
    assets=[dict(r) for r in connection.execute("""SELECT a.*,u.source_card_id FROM assets a
        JOIN crawl_urls u USING(crawl_url_id) WHERE a.asset_kind='card_image' AND u.source='2kmtcentral'""")]
    asset_groups=defaultdict(list);hash_cards=defaultdict(set)
    for asset in assets:
        asset['verified_valid_png']=False
        if asset['raw_cache_path']:
            try:
                body=verify(asset)
                v=validate_image_bytes(body,filename=asset['raw_cache_path']).to_dict()
                previous=json.loads(asset['metadata_json']).get('validation',{})
                asset['audit_validation']=v
                id_match=re.search(r'/generated/(\d+)i\d+\.png/',asset['asset_url'])
                asset['url_card_id_matches']=bool(id_match and id_match.group(1)==asset['source_card_id'])
                asset['verified_valid_png']=bool(v['valid'] and v['actual_format']=='PNG'
                    and not previous.get('placeholder_or_error',False) and asset['url_card_id_matches'])
                if asset['verified_valid_png']:
                    hash_cards[asset['content_hash']].add(asset['source_card_id'])
            except Exception as error:
                asset['audit_validation_error']=str(error)
        asset_groups[asset['source_card_id']].append(asset)
    signatures={}
    for domain in ('attributes','tendencies'):
        populated=[tuple(sorted(values(r,domain))) for r in records if values(r,domain)]
        count=Counter(populated)
        signatures[domain]={'modal_keys':list(count.most_common(1)[0][0]) if count else [],
            'modal_card_count':count.most_common(1)[0][1] if count else 0,
            'observed_key_union':sorted(set(k for keys in populated for k in keys)),
            'card_field_count_distribution':dict(sorted(Counter(len(keys) for keys in populated).items()))}
    counts=Counter();counts['known_main_urls']=len(known);counts['distinct_card_ids']=len(groups)
    gap_index=[];text_cards=[]
    for record in records:
        card_id=record['source_card_id'];raw=record.get('raw',{});identity=values(record,'identity')
        record['primary_urls']=groups[card_id];record['assets']=asset_groups[card_id]
        record['all_url_states']=[u for u in urls if u['source_card_id']==card_id]
        good=[a for a in record['assets'] if a['verified_valid_png']]
        record['png_status']='SAVED_VALIDATED' if good else 'MISSING_OR_INVALID'
        record['png_duplicate_review']=[a['content_hash'] for a in good if len(hash_cards[a['content_hash']])>1]
        gaps=[];review=[]
        if good: counts['cards_with_valid_png']+=1
        else: gaps.append('card_png');counts['cards_without_valid_png']+=1
        if not any(p['kind']=='main' for p in record['pages']):
            gaps.append('main_card_page');counts['cards_without_usable_main_page']+=1
        else: counts['cards_with_usable_main_page']+=1
        record['domain_audit']={}
        for domain in ('attributes','tendencies'):
            present=values(record,domain)
            missing=sorted(set(signatures[domain]['modal_keys'])-set(present))
            nulls=[k for k,v in record.get('card',{}).get(domain,{}).get('values',{}).items() if v is None and k!=SKIP]
            collision=conflicts(record,domain)
            invalid={k:v for k,v in present.items() if not isinstance(v,(int,float)) or not 0<=v<=100}
            status='NO_PARSED_VALUES' if not present else 'PARTIAL_VS_OBSERVED_TEMPLATE' if missing or nulls else 'OBSERVED_TEMPLATE_COVERED'
            if collision or invalid: status='REVIEW_CONFLICT_OR_INVALID_VALUE';review.append(domain)
            record['domain_audit'][domain]={'status':status,'field_count':len(present),'missing_vs_observed_template':missing,
                'explicit_nulls':nulls,'conflicting_observations':collision,'out_of_range_or_nonnumeric':invalid}
            counts[domain+'_'+status]+=1
            if not present or missing or nulls:gaps.append(domain)
        badge_values=values(record,'badges');tier_counts=Counter(v for v in badge_values.values() if v in TIERS)
        declared=raw.get('badge_counts',{});expected={t:declared.get(t.replace('_',' ').title()) for t in TIERS}
        expected['HALL_OF_FAME']=declared.get('Hall of Fame')
        all_counts=all(isinstance(v,int) and not isinstance(v,bool) for v in expected.values())
        mismatch={t:{'declared':expected[t],'named':tier_counts[t]} for t in TIERS if expected[t] is not None and expected[t]!=tier_counts[t]}
        badge_conflicts=conflicts(record,'badges')
        unknown={k:v for k,v in badge_values.items() if v not in (*TIERS,'PERSONALITY')}
        if all_counts and all(v==0 for v in expected.values()) and not tier_counts:
            badge_status='CONFIRMED_ZERO_TIERED_BADGES'
        elif all_counts and not mismatch and not badge_conflicts and not unknown:
            badge_status='NAMED_TIERS_MATCH_SOURCE_COUNTS'
        elif not badge_values:
            badge_status='NO_NAMED_BADGE_DATA'
        else:badge_status='INCOMPLETE_OR_CONFLICTING_BADGE_EVIDENCE'
        if badge_status in ('NO_NAMED_BADGE_DATA','INCOMPLETE_OR_CONFLICTING_BADGE_EVIDENCE'):gaps.append('badges')
        if badge_conflicts or mismatch or unknown:review.append('badges')
        badge_page=any(p['kind']=='badges' for p in record['pages'])
        record['badge_audit']={'status':badge_status,'declared_main_counts':expected,'named_tier_counts':dict(tier_counts),
            'count_mismatches':mismatch,'conflicting_observations':badge_conflicts,'unknown_levels':unknown,
            'personality_names':[k for k,v in badge_values.items() if v=='PERSONALITY'],
            'personality_coverage':'DETAIL_PAGE_SAVED; ABSENT_NAMES_NOT_ASSUMED_MISSING' if badge_page else 'UNVERIFIED_NO_DETAIL_PAGE'}
        counts['badges_'+badge_status]+=1
        counts['named_hof_badge_records']+=tier_counts['HALL_OF_FAME']
        if tier_counts['HALL_OF_FAME']:counts['cards_with_named_hof_badges']+=1
        if not badge_page:counts['cards_without_saved_badge_detail_page']+=1
        missing_identity=[k for k in ('name','overall','tier','collection','team','primary_position','height','weight') if identity.get(k) is None]
        record['missing_core_identity']=missing_identity
        if missing_identity:gaps.append('identity_metadata')
        if raw.get('warnings') or record['errors'] or record['png_duplicate_review']:review.append('source_or_file_evidence')
        record['missing_domains']=sorted(set(gaps));record['review_domains']=sorted(set(review))
        counts['cards_with_confirmed_or_observed_template_gaps']+=bool(gaps)
        counts['cards_needing_review']+=bool(review)
        counts['cards_without_identified_core_gaps']+=not bool(gaps)
        gap_index.append({'source_card_id':card_id,'identity':identity,'primary_urls':[u['url'] for u in groups[card_id]],
            'missing_domains':record['missing_domains'],'review_domains':record['review_domains'],
            'png_status':record['png_status'],'missing_identity':missing_identity,
            'attributes':record['domain_audit']['attributes'],'tendencies':record['domain_audit']['tendencies'],
            'badges':record['badge_audit'],'mtdb_approved':False})
        lines=['='*100,f"CARD {card_id} | {identity.get('name',raw.get('name','UNKNOWN'))} | OVR {identity.get('overall','?')} | {identity.get('tier','?')}",
            'Primary URL(s): '+', '.join(u['url'] for u in groups[card_id]),
            'PNG: '+record['png_status'],
            'Missing domains: '+(', '.join(record['missing_domains']) or 'None identified against observed web template'),
            'Review domains: '+(', '.join(record['review_domains']) or 'None flagged'),
            'Missing core identity: '+(', '.join(missing_identity) or 'None'),
            'Identity / variant facts: '+json.dumps(identity,ensure_ascii=False,sort_keys=True),
            'Saved page evidence (latest verified successful capture per URL, not necessarily latest attempt):']
        for p in record['pages']:
            lines.append(f"  {p['kind']} | capture={p['archive_timestamp']} | snapshot={p['snapshot_id']} | {p['url']}\n    {p['raw_cache_path']}\n    SHA256 {p['content_hash']}")
        lines.append('All discovered URL states:')
        for u in record['all_url_states']:lines.append(f"  {u['fetch_status']} | {u['url']} | last error: {u['last_error'] or '-'}")
        lines.append('Image evidence:')
        if not record['assets']:lines.append('  No registered card-image asset; image URL may also be missing from the page.')
        for a in record['assets']:
            lines.append(f"  ledger={a['fetch_status']} | audit_valid_png={a['verified_valid_png']} | {a['asset_url']}\n    file={a['raw_cache_path'] or 'NOT SAVED'}\n    SHA256={a['content_hash'] or 'NONE'}")
            if a.get('audit_validation'):lines.append('    validation='+json.dumps(a['audit_validation'],sort_keys=True))
            if a.get('audit_validation_error'):lines.append('    error='+a['audit_validation_error'])
        if record['png_duplicate_review']:lines.append('  REVIEW shared PNG hash across different card IDs: '+', '.join(record['png_duplicate_review']))
        for domain in ('attributes','tendencies'):
            lines.append(domain.upper()+' AUDIT: '+json.dumps(record['domain_audit'][domain],ensure_ascii=False,sort_keys=True))
            present=values(record,domain)
            lines.extend('  '+k+' = '+str(v) for k,v in sorted(present.items()))
            if not present:lines.append('  NO PARSED VALUES; this is not a claim that the player has zero attributes/tendencies.')
        lines.append('BADGES AUDIT: '+json.dumps(record['badge_audit'],ensure_ascii=False,sort_keys=True))
        lines.extend('  '+k+' = '+str(v) for k,v in sorted(badge_values.items()))
        for domain in ('hot_zones','gameplay_metadata','signatures','gear'):
            lines.append(domain.upper()+': '+(json.dumps(values(record,domain),ensure_ascii=False,sort_keys=True) if values(record,domain) else 'NO WEB VALUES RECOVERED IN THIS AUDIT'))
        lines.append('Other image URLs (not automatically saved): '+json.dumps({k:raw.get(k) for k in ('card_image_url','player_image_url')}))
        lines.append('Raw metadata: '+json.dumps(raw.get('raw_metadata',{}),ensure_ascii=False,sort_keys=True))
        lines.append('Parser warnings: '+json.dumps(raw.get('warnings',[]),ensure_ascii=False))
        lines.append('Page read/parse errors: '+json.dumps(record['errors'],ensure_ascii=False))
        text_cards.append('\n'.join(lines))
    supplementary={}
    supplementary['mtdb_pages']=[dict(r) for r in connection.execute("SELECT u.url,u.fetch_status,s.raw_cache_path,s.content_hash,s.archive_timestamp FROM crawl_urls u LEFT JOIN page_snapshots s ON s.snapshot_id=u.latest_snapshot_id WHERE u.source='mtdb'")]
    supplementary['mtdb_assets']=[dict(r) for r in connection.execute("SELECT asset_url,fetch_status,raw_cache_path,content_hash FROM assets WHERE asset_kind='mtdb_card_image'")]
    counts['selected_hash_verified_page_snapshots']=sum(len(r['pages']) for r in records)
    counts['saved_badge_page_urls']=sum(sum(p['kind']=='badges' for p in r['pages']) for r in records)
    counts['saved_tendency_page_urls']=sum(sum(p['kind']=='tendencies' for p in r['pages']) for r in records)
    counts['selected_page_read_parse_errors']=sum(len(r['errors']) for r in records)
    counts['snapshot_integrity_errors']=len(hash_errors)
    counts['raw_png_files_all_sources']=sum(1 for _ in ROOT.rglob('*.png'))
    counts['valid_primary_png_unique_files']=len({a['raw_cache_path'] for a in assets if a['verified_valid_png']})
    counts['valid_primary_png_asset_rows']=sum(a['verified_valid_png'] for a in assets)
    summary={'generated_local':datetime.now().astimezone().isoformat(),'counts':dict(counts),
        'cancelled':cancelled,'observed_templates':signatures,'hash_errors':hash_errors,
        'image_status_ledger':dict(Counter(a['fetch_status'] for a in assets)),
        'duplicate_primary_png_hashes':{h:sorted(ids) for h,ids in hash_cards.items() if len(ids)>1},
        'mtdb_supplementary':supplementary,'app_catalog_unchanged':app_before=={'size':app.stat().st_size,'mtime_ns':app.stat().st_mtime_ns}}
    save(OUT/'audit_summary.json',summary)
    save(OUT/'audit_cards.json',records)
    save(OUT/'missing_only_review_queue.json',gap_index)
    intro=['NBA 2K17 MyTEAM - FINAL 2KMTCENTRAL SAVED-EVIDENCE AUDIT',
        'Generated: '+summary['generated_local'],
        'Scope: 2KMTCentral collection is STOPPED BY USER. No more primary retries. MTDB is PAUSED pending audit review and approval.',
        'This report is offline. It neither fetched URLs nor rebuilt/updated cards.sqlite or the app catalog.',
        '', 'READ THIS BEFORE USING THE COUNTS',
        '* Resolved crawl jobs include unavailable/archive-missing results; resolved does NOT mean data saved.',
        '* Main URLs can alias the same card ID. Per-card sections below group aliases rather than inventing duplicate cards.',
        '* Every selected page and saved image is SHA256 checked. A failed latest attempt does not erase an older valid saved page.',
        '* A saved HTML page is not proof of complete data. Parsed field presence, explicit gaps, count mismatches and conflicts are listed.',
        '* Attribute/tendency missing-label checks use the MODAL field template observed in the saved 2K17 pages, not 2K16 counts.',
        '* Template coverage is not a verified complete NBA 2K17 game-memory schema. No defaults or guessed values fill missing fields.',
        '* Source rows are selected by latest successful archive timestamp per URL. Different page timestamps can conflict; conflicts require review.',
        '* Tiered badges: Bronze, Silver, Gold, Hall of Fame (purple). Four explicit zero source counts mean no tiered badges, not missing data.',
        '* Personality badges are separate. Unlisted badge names are NOT automatically treated as missing; membership can be card-specific.',
        '* Missing metadata can be absent from the website or not applicable; absent optional values do not justify automatic MTDB scanning.',
        '* Native signatures, appearance, roster links, gear and injection offsets are separate from website recovery. No injection readiness is asserted.',
        '* MTDB pages/PNGs collected before the scope correction are inventoried separately and have NOT filled any primary card here.',
        '* MTDB queue is REVIEW ONLY, not an approved fetch list. Match exact player+OVR+tier+theme/collection/variant; IDs are not interchangeable.',
        '* For a partial card, request only genuinely missing domains after matching. Never overwrite verified primary values; no broad MTDB pass.',
        '', 'SUMMARY TOTALS',json.dumps(dict(counts),indent=2,sort_keys=True),
        '', 'PRIMARY IMAGE LEDGER (status may differ from audit-verified file availability)',json.dumps(summary['image_status_ledger'],indent=2,sort_keys=True),
        '', 'OBSERVED 2K17 WEB FIELD TEMPLATES (comparison reference, not game-schema certification)',json.dumps(signatures,indent=2,sort_keys=True),
        '', 'CANCELLED FINAL JOBS AND OTHER UNRESOLVED JOBS',json.dumps(cancelled,indent=2),
        '', 'SOURCE/CACHE INTEGRITY ISSUES',json.dumps(hash_errors,indent=2),
        '', 'SUPPLEMENTARY MTDB EVIDENCE - SEPARATE, UNMATCHED, NO PRIMARY GAP CREDIT',json.dumps(supplementary,indent=2),
        '', 'MISSING / REVIEW INDEX - ONE LINE PER CARD (MTDB NOT AUTHORIZED)',
        *[f"{r['source_card_id']} | {r['identity'].get('name','UNKNOWN')} | OVR {r['identity'].get('overall','?')} | missing={','.join(r['missing_domains']) or '-'} | review={','.join(r['review_domains']) or '-'}" for r in gap_index],
        '', 'DETAILED PER-CARD AUDIT - AVAILABLE VALUES, MISSING VALUES, SOURCE FILES',*text_cards]
    destination=OUT/'NBA2K17_FINAL_2KMTCENTRAL_AUDIT_2026-09-07.txt'
    destination.write_text('\n'.join(intro)+'\n',encoding='utf-8-sig')
    progress('audit_finished',report=str(destination),counts=dict(counts),app_catalog_unchanged=summary['app_catalog_unchanged'])


if __name__ == '__main__':
    run()
