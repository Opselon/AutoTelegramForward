# Web bundle: initial payload after code splitting

Measured from the built `dist/index.html`, i.e. what a first visit actually
downloads before any interaction (`gzip -6`, the level the CDN serves):

| request | raw | gzip |
|---|---|---|
| `react-*.js` (vendor) | 206.0 KB | **63.6 KB** |
| `index-*.js` (shell + router + primitives) | 139.8 KB | **41.9 KB** |
| `icons-*.js` | 27.4 KB | **9.7 KB** |
| `index-*.css` (tokens + primitives) | 32.3 KB | **7.2 KB** |
| `rolldown-runtime-*.js` | 0.6 KB | **0.4 KB** |
| **initial total** | **406.1 KB** | **122.8 KB** |

Before: **1045 KB raw / 308 KB gz in a single chunk**, largely one 224 KB
`App.tsx`. After: **27 JS chunks**, largest 63.6 KB gz; every panel below the
fold is a `React.lazy` chunk and each locale is its own chunk.

Budget (`docs/ui-ux-system.md` §5): initial ≤ 130 KB gz · no chunk > 200 KB gz ·
≥ 6 chunks. All three hold.

Reproduce:

```
cd web/frontend && npm run build
python3 - <<'PY'
import re, gzip, pathlib
html = pathlib.Path('dist/index.html').read_text()
refs = sorted(set(re.findall(r'(?:src|href)="(/assets/[^"]+)"', html)))
raw = gz = 0
for r in refs:
    b = pathlib.Path('dist' + r).read_bytes(); raw += len(b); gz += len(gzip.compress(b, 6))
print(len(refs), 'requests', round(raw/1024,1), 'KB raw', round(gz/1024,1), 'KB gz')
PY
```
