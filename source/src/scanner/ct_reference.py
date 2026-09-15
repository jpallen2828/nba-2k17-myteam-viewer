"""Static Cheat Engine XML reference importer. Never evaluates CE, AA or Lua code.

Definitions are candidates, not permissions to read or write a process. In CE's
XML pointer offsets are stored outermost first; resolution uses reverse order.
Saved LastState addresses and user symbols are historical diagnostics only.
"""
from collections import Counter, defaultdict
from hashlib import sha256
import math
from pathlib import Path
import re
import struct
import xml.etree.ElementTree as ET


def label(text):
    return (text or '').strip().strip('"')


def hex_literal(text):
    """CE Offset text is hexadecimal; expressions remain unresolved strings."""
    if text and re.fullmatch(r'-?(?:0x)?[0-9a-fA-F]+', text.strip()):
        return int(text, 16)
    return None


def xml_data(node):
    return {'tag': node.tag, 'attributes': dict(node.attrib), 'text': node.text or '',
            'children': [xml_data(c) for c in node]}


def parse_table(path, source_url=None):
    raw = Path(path).read_bytes()
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('DTD/entity declarations are not accepted')
    root = ET.fromstring(raw)
    if root.tag != 'CheatTable':
        raise ValueError('Expected CheatTable XML')
    entries, scripts = [], []

    def visit(container, parents):
        for ordinal, node in enumerate(container.findall('CheatEntry')):
            uid = '/'.join([p['ordinal'] for p in parents] + [str(ordinal)])
            desc = label(node.findtext('Description'))
            addr = node.findtext('Address')
            offsets = [o.text or '' for o in node.findall('./Offsets/Offset')]
            nums = [hex_literal(o) for o in offsets]
            dtype = node.findtext('VariableType')
            start, bits = node.findtext('BitStart'), node.findtext('BitLength')
            start, bits = (int(start) if start is not None else None), (int(bits) if bits is not None else None)
            size = {'Byte': 1, '2 Bytes': 2, '4 Bytes': 4, '8 Bytes': 8, 'Float': 4, 'Double': 8}.get(dtype)
            if dtype == 'Binary' and bits is not None and start is not None:
                size = math.ceil((bits + start) / 8)
            if dtype == 'Array of byte':
                size = int(node.findtext('ByteLength', '0'))
            if dtype == 'String':
                size = int(node.findtext('Length', '0')) * (2 if node.findtext('Unicode') == '1' else 1)
            dd = node.find('DropDownList')
            options = []
            if dd is not None:
                for line in (dd.text or '').splitlines():
                    if not line.strip():
                        continue
                    value, sep, option_label = line.partition(':')
                    # Do not guess decimal/hex for a dropdown's values: CE flags,
                    # byte arrays and textual aliases may affect interpretation.
                    options.append({'value_text': value, 'label': option_label if sep else None, 'raw': line})
            context = parents[1]['description'] if len(parents) > 1 else None
            player = addr in ('mcdz', 'lydz', 'dfdz', 'bjdz', 'mcsg')
            candidate = nums[0] if player and len(nums) == 1 else None
            item = dict(uid=uid, id=node.findtext('ID'), label=desc,
                        context=context, context_path=[p['description'] for p in parents],
                        parent_uids=[p['uid'] for p in parents], data_type=dtype,
                        address_expression=addr, pointer_offsets_xml=offsets,
                        pointer_offsets_numbers=nums, pointer_offsets_resolution_order=list(reversed(offsets)),
                        byte_length=size, bit_start=start, bit_width=bits,
                        bit_mask=((1 << bits)-1) << start if bits is not None and start is not None else None,
                        player_context=player, context_relative_offset=candidate,
                        canonical_record_offset_candidate=candidate if addr in ('mcdz','lydz','dfdz','bjdz') and candidate is not None and 0 <= candidate and size is not None and candidate+size <= 0x460 else None,
                        dropdown_attributes=dict(dd.attrib) if dd is not None else {}, dropdown_values=options,
                        properties={c.tag: xml_data(c) for c in node if c.tag not in ('CheatEntries','AssemblerScript','LuaScript')},
                        status='REFERENCE_ONLY_UNVALIDATED', write_enabled=False)
            for script_node in node:
                if script_node.tag in ('AssemblerScript', 'LuaScript'):
                    text = script_node.text or ''
                    scripts.append({'entry_uid': uid, 'kind': script_node.tag, 'text': text,
                                    'sha256': sha256(text.encode()).hexdigest(), 'executed': False})
            entries.append(item)
            child = node.find('CheatEntries')
            if child is not None:
                visit(child, parents + [{'uid':uid, 'ordinal':str(ordinal), 'description':desc}])
    visit(root.find('CheatEntries'), [])
    symbols = []
    # Deliberately narrow syntactic recognition, not an assembler interpreter.
    for script in scripts:
        for m in re.finditer(r'mov\s+rax,\s*(nba2k17\.exe)\s*\n\s*add\s+rax,\s*([0-9A-Fa-f]+)\s*\n\s*mov\s+\[([\w]+)\],\s*rax', script['text'], re.I):
            symbols.append({'symbol':m[3], 'module':m[1], 'rva':int(m[2],16),
                            'operation':'symbol storage contains module_base + rva',
                            'source_script_sha256':script['sha256'], 'source_text':m[0],
                            'compatible_with_current_build':None})
    by_layout = defaultdict(list)
    for e in entries:
        if e['player_context'] and e['context_relative_offset'] is not None:
            key = (tuple(e['pointer_offsets_xml']), e['data_type'], e['byte_length'], e['bit_start'], e['bit_width'])
            by_layout[str(key)].append(e['uid'])
    return dict(format='nba2k17-static-ct-reference/v1', source_url=source_url,
                source_sha256=sha256(raw).hexdigest(), source_bytes=len(raw),
                table_version=root.get('CheatEngineTableVersion'), entry_count=len(entries),
                type_counts=dict(Counter(e['data_type'] or 'Group' for e in entries)),
                context_counts=dict(Counter(e['context'] or 'root' for e in entries)),
                entries=entries, scripts=scripts, static_module_symbols=symbols,
                matching_layout_groups=[v for v in by_layout.values() if len(v)>1],
                other_xml=[xml_data(c) for c in root if c.tag!='CheatEntries'],
                conversion_logic={'embedded_scripts_executed':False,
                    'custom_types':[xml_data(c) for c in root.findall('./CustomTypes/*')],
                    'note':'Byte fields remain raw. Display conversions in labels/dropdowns are retained as text; no formula or script is evaluated.'},
                limitations=['Root symbols belong to the Update 2 table; they are not current-process addresses.',
                    'PLAYER EDIT is an editor buffer context. Its inline fields need independent canonical-record and linked-storage validation.',
                    'MC hooks and captured registers are recorded only as script text; not installed or resolved.',
                    'Equal offsets across contexts or plausible values do not authorize writes.'])


def decode_field(data, definition, *, offset=None):
    """Decode an explicitly supplied byte buffer only. No pointer chasing."""
    off = definition['context_relative_offset'] if offset is None else offset
    n = definition['byte_length']
    if off is None or n is None or n <= 0 or off < 0 or off+n > len(data):
        raise ValueError('Field is not wholly inside the supplied context buffer')
    block = data[off:off+n]
    dtype = definition['data_type']
    if dtype == 'Binary':
        return (int.from_bytes(block,'little') >> definition['bit_start']) & ((1 << definition['bit_width'])-1)
    if dtype in ('Float','Double'):
        return struct.unpack('<f' if dtype=='Float' else '<d',block)[0]
    if dtype in ('Byte','2 Bytes','4 Bytes','8 Bytes'):
        signed = definition['properties'].get('ShowAsSigned',{}).get('text') == '1'
        return int.from_bytes(block,'little',signed=signed)
    if dtype == 'Array of byte':
        return block.hex()
    if dtype == 'String':
        unicode = definition['properties'].get('Unicode',{}).get('text') == '1'
        text = block.decode('utf-16-le' if unicode else 'utf-8',errors='replace')
        return text.split('\0',1)[0] if definition['properties'].get('ZeroTerminate',{}).get('text')=='1' else text
    raise ValueError('Unsupported reference type: '+str(dtype))
