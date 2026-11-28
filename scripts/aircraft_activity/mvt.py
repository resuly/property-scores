"""Minimal Mapbox Vector Tile (MVT 2.x) decoder, standard library only.

decode(bytes) -> {layer_name: [{'type': 1|2|3, 'tags': {...}, 'geom': [[(x, y), ...], ...], 'extent': int}]}
Coordinates are tile-local integers (0..extent). to_lonlat() converts them for a z/x/y tile.
Input is treated as untrusted data: only parsed, bounded by its own length.
"""
import gzip, math, struct


def _varint(b, i):
    r = s = 0
    while True:
        c = b[i]; i += 1
        r |= (c & 0x7F) << s
        if c < 0x80:
            return r, i
        s += 7
        if s > 63:
            raise ValueError('bad varint')


def _fields(b):
    i, n = 0, len(b)
    while i < n:
        key, i = _varint(b, i)
        f, w = key >> 3, key & 7
        if w == 0:
            v, i = _varint(b, i)
        elif w == 1:
            v = b[i:i + 8]; i += 8
        elif w == 2:
            L, i = _varint(b, i); v = b[i:i + L]; i += L
        elif w == 5:
            v = b[i:i + 4]; i += 4
        else:
            raise ValueError(f'wire type {w}')
        yield f, w, v


def _packed(b):
    out, i = [], 0
    while i < len(b):
        v, i = _varint(b, i); out.append(v)
    return out


def _value(b):
    for f, w, v in _fields(b):
        if f == 1: return bytes(v).decode('utf-8', 'replace')
        if f == 2: return struct.unpack('<f', v)[0]
        if f == 3: return struct.unpack('<d', v)[0]
        if f in (4, 5): return v
        if f == 6: return (v >> 1) ^ -(v & 1)
        if f == 7: return bool(v)
    return None


def _geom(cmds):
    parts, cur, x, y, i = [], None, 0, 0, 0
    while i < len(cmds):
        c = cmds[i]; i += 1
        cid, cnt = c & 7, c >> 3
        if cid in (1, 2):
            for _ in range(cnt):
                dx, dy = cmds[i], cmds[i + 1]; i += 2
                x += (dx >> 1) ^ -(dx & 1); y += (dy >> 1) ^ -(dy & 1)
                if cid == 1:
                    cur = [(x, y)]; parts.append(cur)
                else:
                    cur.append((x, y))
        elif cid == 7 and cur:
            cur.append(cur[0])
    return parts


def decode(data):
    if data[:2] == b'\x1f\x8b':
        data = gzip.decompress(data)
    out = {}
    for f, w, lb in _fields(memoryview(data)):
        if f != 3:
            continue
        name, keys, vals, feats, extent = None, [], [], [], 4096
        for lf, lw, lv in _fields(lb):
            if lf == 1: name = bytes(lv).decode()
            elif lf == 3: keys.append(bytes(lv).decode())
            elif lf == 4: vals.append(_value(lv))
            elif lf == 2: feats.append(lv)
            elif lf == 5: extent = lv
        rows = []
        for fb in feats:
            tags, typ, g = [], 0, []
            for ff, fw, fv in _fields(fb):
                if ff == 2: tags = _packed(fv)
                elif ff == 3: typ = fv
                elif ff == 4: g = _packed(fv)
            rows.append({'type': typ, 'tags': {keys[tags[k]]: vals[tags[k + 1]] for k in range(0, len(tags) - 1, 2)},
                         'geom': _geom(g), 'extent': extent})
        out[name] = rows
    return out


def tile_of(lat, lon, z):
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


def to_lonlat(z, x, y, px, py, extent):
    n = 2 ** z
    lon = (x + px / extent) / n * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + py / extent) / n))))
    return lat, lon
