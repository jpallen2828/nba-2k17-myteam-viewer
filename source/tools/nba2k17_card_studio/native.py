"""Read 2K text resources without discarding duplicate keys or omitted values."""
import json
import re
import struct


class Object(dict):
    def __init__(self, pairs):
        super().__init__(pairs)
        self.pairs = pairs


def parse(text):
    tokens = re.findall(r'"(?:\\.|[^"\\])*"|[{}\[\]:,]|-?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?|\w+', text)
    i = 0
    def value():
        nonlocal i
        token = tokens[i]; i += 1
        if token == '{':
            pairs = []
            while tokens[i] != '}':
                key = json.loads(tokens[i]); i += 1
                result = None
                if tokens[i] == ':':
                    i += 1
                    if tokens[i] not in {',', '}'}: result = value()
                pairs.append((key, result))
                if tokens[i] == ',': i += 1
            i += 1
            return Object(pairs)
        if token == '[':
            items = []
            while tokens[i] != ']':
                items.append(value())
                if tokens[i] == ',': i += 1
            i += 1
            return items
        return json.loads(token)
    # Resources have an implicit outer object: "resource": { ... }.
    tokens = ['{'] + tokens + ['}']
    result = value()
    if i != len(tokens): raise ValueError('Unparsed native resource tokens')
    return result


def chunks(data):
    magic, size = struct.unpack_from('<II', data)
    if magic != 0xb2463c45 or size != len(data): raise ValueError('Unsupported model header')
    offset=8
    result=[]
    while offset<len(data):
        tag, length, content_hash, semantic, encoding = struct.unpack_from('<IIQII',data,offset)
        if length<24 or offset+length>len(data): raise ValueError('Invalid model chunk bounds')
        result.append({'offset':offset,'tag':tag,'length':length,'hash':content_hash,
                       'semantic':semantic,'encoding':encoding,'data':data[offset+24:offset+length]})
        offset+=length
    return result
