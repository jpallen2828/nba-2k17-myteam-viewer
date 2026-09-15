"""Offline portrait experiment plans; no live writes or refresh calls."""
from hashlib import sha256
from scanner.player_codec import preview_record


def experiment_preview(destination, source, *, include_version=False, asset_verified=False):
    if not asset_verified:
        raise ValueError('Source portrait asset has not been verified; no destination fallback is allowed.')
    values=source.get('values',{})
    portrait=values.get('portrait_0x2c0_u32')
    version=values.get('version_0xd4_u16')
    if type(portrait) is not int or not 0<=portrait<=0xffffffff:
        raise ValueError('Missing or invalid exact source portrait value')
    if not source.get('source_record_sha256') or not source.get('capture_sha256'):
        raise ValueError('Missing exact source capture binding')
    evidence=[{'sha256':source['capture_sha256'],'source_record_sha256':source['source_record_sha256']}]
    fields=[{'field':'gameplay_metadata.portrait_id','offset':0x2c0,'size':4,
             'codec':'uint','confidence':'NATIVE_SCHEMA_WIDTH_EXPERIMENT','evidence':evidence}]
    requested={'portrait_id':portrait}
    if include_version:
        if type(version) is not int or not 0<=version<=0xffff:
            raise ValueError('Missing or invalid exact source version value')
        fields.append({'field':'gameplay_metadata.version_identity_candidate','offset':0xd4,'size':2,
                       'codec':'uint','confidence':'CONTROLLED_EXPERIMENT_NOT_PORTRAIT_PROVEN','evidence':evidence})
        requested['version_identity_candidate']=version
    result=preview_record(destination,{'gameplay_metadata':requested},{'fields':fields})
    after=bytes.fromhex(result['after_hex'])
    assert after[0xd6:0xd8]==destination[0xd6:0xd8]
    result.update(source_record_sha256=source['source_record_sha256'],
                  commentary_preserved=True,visual_success=False,live_approved=False,
                  experiment='portrait_and_version' if include_version else 'portrait_only')
    return result


def classify_readback(expected, actual, *, visual_observation=None, reloaded=False):
    """Memory verification and user-visible presentation are independent facts."""
    return {'source_values_written_correctly':expected==actual,
            'intended_portrait_appeared':visual_observation,
            'reload_observed':reloaded,
            'requires_presentation_investigation':expected==actual and visual_observation is False}
