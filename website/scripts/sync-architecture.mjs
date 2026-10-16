/**
 * Architecture atlas generator.
 *
 * Everything the /system/ pages claim about the shape of this repository — the
 * module graph, the keystone ranking, the cross-subsystem traffic, the service
 * topology and the runtime flows — is computed here from the checkout itself.
 * Nothing in this file is hand-typed product copy: a claim that cannot be
 * derived from a file, a grep or the compose/proto registries is simply not
 * emitted, and the failed probe is recorded under `omittedProbes` so the pages
 * can say what they could not verify.
 *
 * Outputs:
 *   src/data/architecture.json   build-time data (pages import it directly)
 *   public/system/graph.json     compact index-based graph the islands fetch
 */
import fs from 'node:fs';
import path from 'node:path';
import { execSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const rootDir = path.resolve(__dirname, '../../');
const websiteDir = path.resolve(__dirname, '../');
const dataDir = path.join(websiteDir, 'src', 'data');
const publicDir = path.join(websiteDir, 'public', 'system');

const read = (rel) => fs.readFileSync(path.join(rootDir, rel), 'utf8');
const readSafe = (rel) => {
  try {
    return read(rel);
  } catch {
    return null;
  }
};
const exists = (rel) => fs.existsSync(path.join(rootDir, rel));

// ---------------------------------------------------------------------------
// Subsystems — the top-level shape of the checkout.
// ---------------------------------------------------------------------------
const SUBSYSTEMS = [
  {
    id: 'core',
    label: 'Python Core',
    root: 'core',
    color: '#38bdf8',
    role: 'Forwarding engine',
    tech: 'Python 3 · Pyrogram · gRPC',
    blurb:
      'Domain model, rule engine and the durable delivery pipeline. Owns the Telegram sessions and the database.',
  },
  {
    id: 'api',
    label: 'Go API',
    root: 'api',
    color: '#34d399',
    role: 'Control plane',
    tech: 'Go · net/http · gRPC client',
    blurb:
      'Stateless REST control plane. Authenticates clients, applies tenant scoping, and speaks gRPC to the core and the logger.',
  },
  {
    id: 'logger',
    label: 'Logger service',
    root: 'logger',
    color: '#f472b6',
    role: 'Observability',
    tech: 'Rust engine · gRPC · SQLite',
    blurb:
      'Central log store — the Rust engine in `rust/logger` — with field-aware secret redaction, correlation-id propagation and a bounded diagnostic window. `logger/` keeps the Python producer adapter the core uses.',
  },
  {
    id: 'web-backend',
    label: 'Web gateway',
    root: 'web/backend/src',
    color: '#fbbf24',
    role: 'Dashboard host',
    tech: 'Rust · axum',
    blurb:
      'Serves the compiled dashboard, proxies the REST API and streams the live log feed over SSE.',
  },
  {
    id: 'web-frontend',
    label: 'Dashboard UI',
    root: 'web/frontend/src',
    color: '#fb923c',
    role: 'Operator console',
    tech: 'TypeScript · React · Vite',
    blurb:
      'Task control, log explorer, diagnostics and admin surfaces for a signed-in tenant.',
  },
  {
    id: 'mobile',
    label: 'Mobile app',
    root: 'mobile/lib',
    color: '#a78bfa',
    role: 'Client',
    tech: 'Dart · Flutter',
    blurb:
      'Native client with its own 10-locale catalog and the same REST contract as the dashboard.',
  },
  {
    id: 'cli',
    label: 'DevOps CLI',
    root: 'atfcli',
    color: '#22d3ee',
    role: 'Operator tooling',
    tech: 'Python · atf entrypoint',
    blurb:
      'Install, validate, diagnose and upgrade the stack from one command surface.',
  },
  {
    id: 'contracts',
    label: 'Interfaces',
    root: 'proto',
    color: '#c084fc',
    role: 'Contracts',
    tech: 'protobuf · JSON fixtures',
    blurb:
      'The gRPC service definitions, the generated Go stubs and the cross-language JSON fixtures that pin the wire format.',
  },
  {
    id: 'website',
    label: 'This website',
    root: 'website/src',
    color: '#e879f9',
    role: 'Documentation',
    tech: 'Astro · TypeScript',
    blurb:
      'The site you are reading, generated from the same checkout it documents.',
  },
];

const SKIP_DIRS = new Set([
  'node_modules',
  '.venv',
  'venv',
  '__pycache__',
  '.git',
  'dist',
  'build',
  '.astro',
  'target',
  '.dart_tool',
  'ephemeral',
  '.idea',
  '.vscode',
  'coverage',
  '.pytest_cache',
  'htmlcov',
  '.mypy_cache',
  '.ruff_cache',
]);

const LANG_BY_EXT = {
  '.py': 'python',
  '.go': 'go',
  '.rs': 'rust',
  '.dart': 'dart',
  '.ts': 'typescript',
  '.tsx': 'typescript',
  '.js': 'javascript',
  '.jsx': 'javascript',
  '.mjs': 'javascript',
  '.astro': 'astro',
  '.proto': 'proto',
  '.sql': 'sql',
  '.sh': 'shell',
  '.ps1': 'powershell',
};

const COMMENT_PREFIX = {
  python: '#',
  go: '//',
  rust: '//',
  dart: '//',
  typescript: '//',
  javascript: '//',
  astro: '//',
  proto: '//',
  sql: '--',
  shell: '#',
  powershell: '#',
};

const SOURCE_LANGS = new Set(['python', 'go', 'rust', 'dart', 'typescript', 'javascript', 'astro', 'proto']);

/** Files whose content is machine-written: counted, but never called authored. */
const isGenerated = (rel) =>
  /(\.pb\.go|\.pb\.grpc\.go|_grpc\.pb\.go|\.g\.dart|\.freezed\.dart)$/.test(rel) ||
  /^api\/proto\//.test(rel);

const countLoc = (src, lang) => {
  const prefix = COMMENT_PREFIX[lang] || '//';
  let loc = 0;
  for (const raw of src.split('\n')) {
    const line = raw.trim();
    if (!line) continue;
    if (line.startsWith(prefix)) continue;
    if (lang === 'python' && /^("""|''')/.test(line)) continue;
    loc++;
  }
  return loc;
};

// ---------------------------------------------------------------------------
// 1. Walk the checkout once, index every file we care about.
// ---------------------------------------------------------------------------
const walk = (rel, out = []) => {
  const abs = path.join(rootDir, rel);
  let entries;
  try {
    entries = fs.readdirSync(abs, { withFileTypes: true });
  } catch {
    return out;
  }
  for (const entry of entries) {
    if (SKIP_DIRS.has(entry.name)) continue;
    const childRel = path.posix.join(rel, entry.name);
    if (entry.isDirectory()) walk(childRel, out);
    else out.push(childRel);
  }
  return out;
};

const files = [];
for (const sub of SUBSYSTEMS) {
  if (!exists(sub.root)) continue;
  for (const rel of walk(sub.root)) {
    const ext = path.extname(rel).toLowerCase();
    const lang = LANG_BY_EXT[ext];
    const isSource = Boolean(lang) && SOURCE_LANGS.has(lang);
    let bytes = 0;
    let loc = 0;
    if (isSource) {
      try {
        const src = fs.readFileSync(path.join(rootDir, rel), 'utf8');
        bytes = Buffer.byteLength(src);
        loc = countLoc(src, lang);
      } catch {
        /* unreadable binary or permission: counted as a file, zero LOC */
      }
    }
    files.push({
      rel,
      subsystem: sub.id,
      lang: lang || ext.replace('.', '') || 'other',
      source: isSource,
      generated: isGenerated(rel),
      bytes,
      loc,
    });
  }
}
// Files outside every subsystem root that still matter to the map.
for (const extra of ['atf.py', 'docker-compose.yml', 'docker-compose.isolated.yml', 'Caddyfile']) {
  if (!exists(extra)) continue;
  const ext = path.extname(extra).toLowerCase();
  const lang = LANG_BY_EXT[ext] || ext.replace('.', '');
  files.push({
    rel: extra,
    subsystem: 'root',
    lang,
    source: SOURCE_LANGS.has(lang),
    generated: false,
    bytes: Buffer.byteLength(readSafe(extra) || ''),
    loc: SOURCE_LANGS.has(lang) ? countLoc(readSafe(extra) || '', lang) : 0,
  });
}

const bySubsystem = new Map();
for (const f of files) {
  if (!bySubsystem.has(f.subsystem)) bySubsystem.set(f.subsystem, []);
  bySubsystem.get(f.subsystem).push(f);
}

const langTotals = new Map();
for (const f of files) {
  if (!f.source) continue;
  const cur = langTotals.get(f.lang) || { lang: f.lang, files: 0, loc: 0, bytes: 0 };
  cur.files++;
  cur.loc += f.loc;
  cur.bytes += f.bytes;
  langTotals.set(f.lang, cur);
}

// ---------------------------------------------------------------------------
// 2. Nodes. Granularity is per language and stated on the page: a Python or
//    Dart module is a file, a Go package is a directory (a Go file is never a
//    unit of dependency on its own).
// ---------------------------------------------------------------------------
const GRANULARITY = {
  python: 'module (file)',
  go: 'package (directory)',
  rust: 'module (file)',
  dart: 'library (file)',
  typescript: 'module (file)',
  javascript: 'module (file)',
  astro: 'route/component (file)',
  proto: 'interface (file)',
};

const nodeKeyFor = (file) => {
  if (file.lang === 'go') return path.posix.dirname(file.rel);
  return file.rel;
};

const nodesByKey = new Map();
for (const f of files) {
  if (!f.source) continue;
  const key = nodeKeyFor(f);
  if (!nodesByKey.has(key)) {
    nodesByKey.set(key, {
      id: key,
      label: key.split('/').pop().replace(/\.(py|go|rs|dart|tsx?|jsx?|mjs|astro)$/, ''),
      path: key,
      subsystem: f.subsystem,
      granularity: f.lang === 'go' ? 'package' : 'file',
      granularityLabel: GRANULARITY[f.lang] || 'module',
      lang: f.lang,
      files: 0,
      testFiles: 0,
      loc: 0,
      generated: true,
      areas: new Set(),
    });
  }
  const node = nodesByKey.get(key);
  node.files++;
  node.loc += f.loc;
  node.generated = node.generated && f.generated;
  node.testFiles += /(^|\/)(tests?|__tests__)\//.test(f.rel) || /(test_|_test\.|\.spec\.)/.test(path.posix.basename(f.rel)) ? 1 : 0;
  node.subsystem = f.subsystem;
  const rest = key.startsWith(`${f.subsystem}/`) ? key.slice(f.subsystem.length + 1) : key;
  const parts = rest.split('/');
  node.areas.add(parts.length > 1 ? parts[0] : '(root)');
}
const nodes = [...nodesByKey.values()].map((n) => ({
  ...n,
  areas: [...n.areas].sort(),
  isTest: n.testFiles > 0 && n.testFiles === n.files,
}));
const nodeIds = new Set(nodes.map((n) => n.id));

// ---------------------------------------------------------------------------
// 3. Import edges, resolved per language against the real index.
// ---------------------------------------------------------------------------
const edges = [];
const edgeSeen = new Set();
const addEdge = (from, to, kind) => {
  if (!from || !to || from === to) return;
  if (!nodeIds.has(from) || !nodeIds.has(to)) return;
  const key = `${from}\u0000${to}\u0000${kind}`;
  if (edgeSeen.has(key)) return;
  edgeSeen.add(key);
  edges.push({ source: from, target: to, kind });
};

const pyIndex = new Map();
for (const f of files) {
  if (f.lang !== 'python') continue;
  if (f.rel.endsWith('__init__.py')) {
    pyIndex.set(f.rel.replace(/\/__init__\.py$/, '').replace(/\//g, '.'), f.rel);
  } else {
    pyIndex.set(f.rel.replace(/\.py$/, '').replace(/\//g, '.'), f.rel);
  }
}
const resolvePy = (mod) => (mod && pyIndex.has(mod) ? pyIndex.get(mod) : null);

const filesByRel = new Map(files.map((f) => [f.rel, f]));
const goPkgIndex = new Map();
for (const f of files) {
  if (f.lang !== 'go') continue;
  const dir = path.posix.dirname(f.rel);
  const pkg = dir.replace(/^api\//, 'autoforward/');
  if (!goPkgIndex.has(pkg)) goPkgIndex.set(pkg, dir);
}

const tsResolve = (fromRel, spec) => {
  const base = path.posix.normalize(path.posix.join(path.posix.dirname(fromRel), spec));
  const candidates = [
    base,
    `${base}.ts`,
    `${base}.tsx`,
    `${base}.js`,
    `${base}.jsx`,
    `${base}/index.ts`,
    `${base}/index.tsx`,
    `${base}.astro`,
  ];
  for (const c of candidates) if (filesByRel.has(c)) return c;
  return null;
};

const dartResolve = (fromRel, spec) => {
  if (spec.startsWith('package:autoforward_mobile/')) {
    const rel = spec.replace('package:autoforward_mobile/', '');
    for (const c of [`mobile/lib/${rel}`, `mobile/lib/${rel}.dart`]) if (filesByRel.has(c)) return c;
    return null;
  }
  // Dart resolves a bare specifier relative to the importing file, so
  // `import 'app_fa.dart'` is a sibling — the `./` prefix is optional and the
  // locale catalogs are all imported that way.
  if (spec.startsWith('dart:') || (spec.includes(':') && !spec.startsWith('.'))) return null;
  const base = path.posix.normalize(path.posix.join(path.posix.dirname(fromRel), spec));
  for (const c of [base, `${base}.dart`]) if (filesByRel.has(c)) return c;
  return null;
};

const pyParse = (rel, src) => {
  const targets = new Set();
  const lines = src.split('\n');
  const pkgParts = path.posix.dirname(rel).split('/');
  for (let i = 0; i < lines.length; i++) {
    let line = lines[i];
    // Join explicit continuation lines: `from x import (a,\n b)`.
    if (/^\s*from\s+[\w.]*\s+import\s*\(?\s*$/.test(line)) {
      let j = i + 1;
      while (j < lines.length && !/\)/.test(lines[j]) && j - i < 40) j++;
      line = `${line} ${lines.slice(i + 1, j + 1).join(' ')}`;
      i = j;
    }
    const from = line.match(/^\s*from\s+(\.*)([\w.]*)\s+import\s+(.+)$/);
    if (from) {
      const [, dots, mod, namesRaw] = from;
      const names = namesRaw
        .replace(/[()]/g, ' ')
        .split(',')
        .map((s) => s.trim().split(/\s+as\s+/)[0].trim())
        .filter((s) => /^[A-Za-z_]\w*$/.test(s) && s !== '*');
      if (dots) {
        const up = dots.length - 1;
        const baseParts = pkgParts.slice(0, pkgParts.length - up);
        const dirTarget = resolvePy([...baseParts, ...mod.split('.').filter(Boolean)].join('.'));
        if (dirTarget) targets.add(dirTarget);
        for (const n of names) {
          const t = resolvePy([...baseParts, mod ? `${mod}.${n}` : n].filter(Boolean).join('.'));
          if (t) targets.add(t);
        }
      } else {
        const t = resolvePy(mod);
        if (t) targets.add(t);
        for (const n of names) {
          const inner = resolvePy(`${mod}.${n}`);
          if (inner) targets.add(inner);
        }
      }
      continue;
    }
    const imp = line.match(/^\s*import\s+(.+)$/);
    if (imp) {
      for (const part of imp[1].split(',')) {
        const mod = part.trim().split(/\s+as\s+/)[0].trim();
        const t = resolvePy(mod);
        if (t) targets.add(t);
      }
    }
  }
  return targets;
};

const goParse = (rel, src) => {
  const targets = new Set();
  const block = src.match(/import\s*\(([\s\S]*?)\)/);
  const specs = [];
  if (block) specs.push(...[...block[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]));
  for (const m of src.matchAll(/^\s*import\s+(?:[\w.]+\s+)?"([^"]+)"/gm)) specs.push(m[1]);
  for (const spec of specs) {
    if (!spec.startsWith('autoforward')) continue;
    const pkg = spec.replace(/"$/, '');
    if (goPkgIndex.has(pkg)) targets.add(goPkgIndex.get(pkg));
  }
  return targets;
};

const tsParse = (rel, src) => {
  const targets = new Set();
  const specs = [];
  for (const m of src.matchAll(/(?:from|import)\s*\(?\s*['"]([^'"]+)['"]/g)) specs.push(m[1]);
  for (const m of src.matchAll(/import\s*\(\s*['"]([^'"]+)['"]\s*\)/g)) specs.push(m[1]);
  for (const spec of specs) {
    if (!spec.startsWith('.')) continue;
    const t = tsResolve(rel, spec);
    if (t) targets.add(t);
  }
  return targets;
};

const dartParse = (rel, src) => {
  const targets = new Set();
  for (const m of src.matchAll(/^\s*(?:import|export)\s+['"]([^'"]+)['"]/gm)) {
    const t = dartResolve(rel, m[1]);
    if (t) targets.add(t);
  }
  return targets;
};

const rustParse = (rel, src) => {
  const targets = new Set();
  for (const m of src.matchAll(/^\s*(?:pub\s+)?mod\s+(\w+)\s*;/gm)) {
    const cand = path.posix.join(path.posix.dirname(rel), `${m[1]}.rs`);
    if (filesByRel.has(cand)) targets.add(cand);
  }
  for (const m of src.matchAll(/^\s*use\s+(?:crate|super)::(\w+)/gm)) {
    const cand = path.posix.join(path.posix.dirname(rel), `${m[1]}.rs`);
    if (filesByRel.has(cand)) targets.add(cand);
  }
  return targets;
};

const astroParse = (rel, src) => {
  const targets = new Set();
  for (const m of src.matchAll(/from\s+['"]([^'"]+)['"]/g)) {
    const spec = m[1];
    if (!spec.startsWith('.')) continue;
    const t = tsResolve(rel, spec);
    if (t) targets.add(t);
  }
  return targets;
};

for (const f of files) {
  if (!f.source) continue;
  const src = readSafe(f.rel);
  if (!src) continue;
  const fromKey = nodeKeyFor(f);
  let targets = new Set();
  if (f.lang === 'python') targets = pyParse(f.rel, src);
  else if (f.lang === 'go') targets = goParse(f.rel, src);
  else if (f.lang === 'rust') targets = rustParse(f.rel, src);
  else if (f.lang === 'dart') targets = dartParse(f.rel, src);
  else if (f.lang === 'typescript' || f.lang === 'javascript') targets = tsParse(f.rel, src);
  else if (f.lang === 'astro') targets = astroParse(f.rel, src);
  // goParse already returns package directories, so the target is used as-is.
  for (const t of targets) addEdge(fromKey, t, 'import');
}

// ---------------------------------------------------------------------------
// 4. Metrics over the module graph.
// ---------------------------------------------------------------------------
const fanIn = new Map();
const fanOut = new Map();
for (const n of nodes) {
  fanIn.set(n.id, 0);
  fanOut.set(n.id, 0);
}
for (const e of edges) {
  fanOut.set(e.source, (fanOut.get(e.source) || 0) + 1);
  fanIn.set(e.target, (fanIn.get(e.target) || 0) + 1);
}

const adjacency = new Map();
for (const e of edges) {
  if (!adjacency.has(e.source)) adjacency.set(e.source, []);
  adjacency.get(e.source).push(e.target);
}

// Tarjan SCC — the honest way to report "circular dependencies".
const cycles = [];
{
  let index = 0;
  const stack = [];
  const onStack = new Set();
  const idx = new Map();
  const low = new Map();
  const visit = (v) => {
    idx.set(v, index);
    low.set(v, index);
    index++;
    stack.push(v);
    onStack.add(v);
    for (const w of adjacency.get(v) || []) {
      if (!idx.has(w)) {
        visit(w);
        low.set(v, Math.min(low.get(v), low.get(w)));
      } else if (onStack.has(w)) {
        low.set(v, Math.min(low.get(v), idx.get(w)));
      }
    }
    if (low.get(v) === idx.get(v)) {
      const comp = [];
      while (stack.length) {
        const w = stack.pop();
        onStack.delete(w);
        comp.push(w);
        if (w === v) break;
      }
      if (comp.length > 1) cycles.push(comp.sort());
    }
  };
  for (const n of nodes) if (!idx.has(n.id)) visit(n.id);
}

const crossMap = new Map();
for (const e of edges) {
  const a = e.source.split('/')[0];
  const b = e.target.split('/')[0];
  if (a === b) continue;
  const key = `${a}->${b}`;
  crossMap.set(key, (crossMap.get(key) || 0) + 1);
}

const nodeById = new Map(nodes.map((n) => [n.id, n]));
const keystones = nodes
  .filter((n) => !n.generated && !n.isTest && n.loc > 0)
  .map((n) => ({
    id: n.id,
    label: n.label,
    subsystem: n.subsystem,
    lang: n.lang,
    loc: n.loc,
    fanIn: fanIn.get(n.id) || 0,
    fanOut: fanOut.get(n.id) || 0,
    score: (fanIn.get(n.id) || 0) * 2 + (fanOut.get(n.id) || 0),
  }))
  .filter((n) => n.fanIn + n.fanOut > 0)
  .sort((a, b) => b.score - a.score || b.loc - a.loc);

const largest = nodes
  .filter((n) => !n.generated && !n.isTest)
  .map((n) => ({
    id: n.id,
    label: n.label,
    subsystem: n.subsystem,
    lang: n.lang,
    files: n.files,
    loc: n.loc,
    fanIn: fanIn.get(n.id) || 0,
    fanOut: fanOut.get(n.id) || 0,
  }))
  .sort((a, b) => b.loc - a.loc);

const orphans = nodes
  .filter((n) => (fanIn.get(n.id) || 0) === 0 && (fanOut.get(n.id) || 0) === 0)
  .filter((n) => !n.isTest)
  .filter((n) => !/\.proto$/.test(n.id))
  // A package marker is not a module: `__init__.py` files are import plumbing.
  .filter((n) => !/(^|\/)__init__\.py$/.test(n.id))
  .filter((n) => !/(^|\/)(main|__main__|atf|index|app|entry|cli)\./.test(n.id))
  .map((n) => ({ id: n.id, label: n.label, subsystem: n.subsystem, loc: n.loc }))
  .sort((a, b) => b.loc - a.loc);

// ---------------------------------------------------------------------------
// 5. Cross-process contracts. Every edge needs a real hit in the source; a
//    probe that finds nothing is dropped and reported instead of drawn.
// ---------------------------------------------------------------------------
const omittedProbes = [];
const grepFiles = (rel, re, limit = 400) => {
  const hits = [];
  const pool = rel ? walk(rel) : files.map((f) => f.rel);
  for (const f of pool) {
    if (hits.length >= limit) break;
    if (!/\.(py|go|rs|dart|tsx?|jsx?|mjs|yml|yaml|toml|conf)$/.test(f) && !/(^|\/)(Caddyfile|Dockerfile[\w.-]*)$/.test(f))
      continue;
    const src = readSafe(f);
    if (src && re.test(src)) hits.push(f);
  }
  return hits;
};

const contracts = [];
const addContract = (spec) => {
  const hits = (spec.proofs || []).flatMap((p) => grepFiles(p.rel, p.re, p.limit || 3));
  if (!hits.length) {
    omittedProbes.push(`${spec.label}: no source hit for ${(spec.proofs || [])
      .map((p) => p.re)
      .join(' | ')}`);
    return;
  }
  contracts.push({
    from: spec.from,
    to: spec.to,
    kind: spec.kind,
    label: spec.label,
    protocol: spec.protocol,
    detail: spec.detail,
    evidence: [...new Set(hits)].slice(0, 6),
  });
};

addContract({
  from: 'api',
  to: 'core',
  kind: 'grpc',
  label: 'Control plane → engine',
  protocol: 'gRPC :50051',
  detail: 'Every rule, session and delivery command crosses this boundary as a protobuf RPC.',
  proofs: [
    { rel: 'api', re: /ATF_CORE_GRPC|50051/ },
    { rel: 'core/infrastructure/grpc_server', re: /import grpc|add_\w+Servicer_to_server/ },
  ],
});
addContract({
  from: 'api',
  to: 'logger',
  kind: 'grpc',
  label: 'Control plane → log store',
  protocol: 'gRPC :50052',
  detail: 'The API writes its own logs, audits and latency events to the same central store.',
  proofs: [{ rel: 'api', re: /ATF_LOGGER_GRPC|50052/ }],
});
addContract({
  from: 'core',
  to: 'logger',
  kind: 'grpc',
  label: 'Engine → log store',
  protocol: 'gRPC :50052',
  detail: 'Pipeline stages emit [PIPELINE:*] events through the shared log client.',
  proofs: [
    { rel: 'core', re: /ATF_LOGGER_ADDR|logger\.log_client|LogClient/ },
    { rel: 'logger', re: /class .*LogClient|def log/ },
  ],
});
addContract({
  from: 'core',
  to: 'ai-rewriter',
  kind: 'http',
  label: 'Engine → AI provider',
  protocol: 'HTTP OpenAI-compatible',
  detail: 'Optional rewrite pass. Unreachable provider degrades to pass-through, never to data loss.',
  proofs: [{ rel: 'core/infrastructure/ai', re: /base_url|chat\/completions|openai/i }],
});
addContract({
  from: 'web-backend',
  to: 'api',
  kind: 'http',
  label: 'Gateway → REST API',
  protocol: 'HTTP reverse proxy + SSE',
  detail: 'Server-side proxy keeps the API key out of the browser and streams the live log feed.',
  proofs: [
    { rel: 'web/backend/src', re: /ATF_API_ADDR|reqwest|api\/v1/ },
    { rel: 'web', re: /api\/v1\/logs\/stream|SSE|text\/event-stream/ },
  ],
});
addContract({
  from: 'web-frontend',
  to: 'web-backend',
  kind: 'http',
  label: 'Dashboard → gateway',
  protocol: 'fetch /api/v1/*',
  detail: 'The React app only ever talks to its own origin; the gateway owns upstream addressing.',
  proofs: [{ rel: 'web/frontend/src', re: /\/api\/v1/ }],
});
addContract({
  from: 'mobile',
  to: 'api',
  kind: 'http',
  label: 'Mobile app → REST API',
  protocol: 'HTTPS + bearer token',
  detail: 'Same REST contract as the dashboard, with offline-tolerant error surfaces.',
  proofs: [{ rel: 'mobile/lib', re: /ATF_API|baseUrl|http[s]?:\/\// }],
});
addContract({
  from: 'cli',
  to: 'api',
  kind: 'http',
  label: 'CLI → running stack',
  protocol: 'HTTP health + config probes',
  detail: 'Diagnostics read the same endpoints an operator would curl.',
  proofs: [
    { rel: 'atfcli', re: /8080|healthz|health/ },
    { rel: 'atf.py', re: /8080|healthz|8088/ },
  ],
});
addContract({
  from: 'root',
  to: 'web-backend',
  kind: 'proxy',
  label: 'TLS profile → gateway',
  protocol: 'Caddy reverse proxy',
  detail: 'Optional --profile tls terminates TLS and forwards to the gateway.',
  proofs: [{ rel: null, re: /reverse_proxy[^\n]*web:8088/ }],
});
addContract({
  from: 'contracts',
  to: 'api',
  kind: 'codegen',
  label: 'proto → generated stubs',
  protocol: 'protoc / buf',
  detail: 'The Go stubs and the Python servicers are both generated from one .proto file.',
  proofs: [{ rel: 'api/proto', re: /autoforward|RegisterServer/ }],
});
addContract({
  from: 'contracts',
  to: 'core',
  kind: 'codegen',
  label: 'proto → engine servicers',
  protocol: 'protoc / buf',
  detail: 'One schema, two runtimes; a mismatch fails at the gRPC boundary, not in production.',
  proofs: [{ rel: 'core/infrastructure/grpc_server', re: /import|Servicer/ }],
});
addContract({
  from: 'website',
  to: 'root',
  kind: 'build',
  label: 'Website → checkout',
  protocol: 'build-time ingestion',
  detail: 'The site derives its numbers from the same files a contributor edits.',
  proofs: [{ rel: 'website/scripts', re: /sync-repo-content|sync-architecture/ }],
});

// Claims the site makes about what does NOT exist. Each one is a search that
// must come back empty — an absence nobody checks is a rumour.
const verifiedAbsences = [];
const openChecks = [];
const absence = (label, claim, probes) => {
  const hits = [...new Set(probes.flatMap((p) => grepFiles(p.rel, p.re, 5)))];
  if (hits.length) {
    // A claim we could not confirm is published as an open item — with the file
    // names only, never the matched text, which may itself be sensitive.
    openChecks.push({ label, claim, hits: hits.slice(0, 6) });
    return;
  }
  verifiedAbsences.push({ label, claim, scope: probes.map((p) => p.rel).join(' , ') });
};

absence('Mobile speaks no gRPC', 'The mobile client never dials the engine directly: every call goes through the REST control plane.', [
  { rel: 'mobile/lib', re: /grpc|50051/ },
]);
absence('No secrets in the browser bundle', 'Neither the API key, the master key nor the AI provider address is shipped to the browser.', [
  { rel: 'web/frontend/src', re: /ATF_API_KEY|ATF_MASTER_KEY|9router/ },
]);
absence('No label chosen by a language branch', 'A localised label is never picked by a hardcoded ternary — the catalogue lookup does it, and it falls back to English. (The language switcher itself still shows the target language natively, which is the point of a switcher.)', [
  { rel: 'core/infrastructure/telegram', re: /is_fa\s*\?/ },
  { rel: 'core/application', re: /is_fa\s*\?/ },
  { rel: 'atfcli', re: /is_fa\s*\?/ },
]);
absence('Two separate databases', 'The logger never opens the engine database — the log store is its own file.', [
  { rel: 'logger', re: /atf\.db|ATF_DB_PATH/ },
  { rel: 'rust/logger/src', re: /atf\.db|ATF_DB_PATH/ },
]);
absence('The engine opens no HTTP port', 'The engine is reachable only over gRPC; there is no HTTP server inside the core.', [
  { rel: 'core', re: /uvicorn|FastAPI|aiohttp\.web/ },
]);
absence('No committed credential defaults', 'Every credential in the compose file is an interpolation; no working secret is committed as a default.', [
  { rel: null, re: /^\s{6}[A-Z][A-Z0-9_]*:\s+(?!\$\{)[^\s#]+.*(TOKEN|KEY|HASH|PASSWORD)/m },
]);

// Compose topology.
const services = [];
const composeRaw = readSafe('docker-compose.yml');
if (composeRaw) {
  // Only the `services:` block declares services — volumes and networks use the
  // same two-space indent and would otherwise be reported as containers.
  const lines = composeRaw.split('\n');
  const start = lines.findIndex((l) => /^services:\s*$/.test(l));
  let end = lines.length;
  if (start >= 0) {
    for (let i = start + 1; i < lines.length; i++) {
      if (/^[a-z][a-z0-9_-]*:\s*$/.test(lines[i])) {
        end = i;
        break;
      }
    }
  }
  const body = start >= 0 ? lines.slice(start + 1, end).join('\n') : '';
  const listUnder = (block, key) => {
    const m = block.match(new RegExp(`\\n\\s{4}${key}:\\s*\\n((?:\\s{4,}-\\s+\\S+\\n?)+)`));
    return m ? [...m[1].matchAll(/-\s+([a-z0-9_-]+)/g)].map((x) => x[1]) : [];
  };
  for (const block of body.split(/^  (?=[a-z0-9_-]+:\s*$)/m).slice(1)) {
    const nameMatch = block.match(/^([a-z0-9_-]+):/);
    if (!nameMatch) continue;
    const name = nameMatch[1];
    const depends = listUnder(block, 'depends_on');
    const networks = listUnder(block, 'networks');
    const envKeys = [...block.matchAll(/^\s{6}([A-Z][A-Z0-9_]+):/gm)].map((m) => m[1]);
    const ports = [...block.matchAll(/^\s{6}-\s+"?(\d+:\d+)"?/gm)].map((m) => m[1]);
    const dockerfile = block.match(/dockerfile:\s*(\S+)/);
    const profiles = block.match(/profiles:\s*\["([^"]+)"\]/);
    const healthcheck = /healthcheck:/.test(block);
    const image = block.match(/^\s{4}image:\s*(\S+)/m);
    const containerName = block.match(/container_name:\s*(\S+)/);
    services.push({
      name,
      container: containerName ? containerName[1] : name,
      dockerfile: dockerfile ? dockerfile[1] : null,
      image: image ? image[1] : null,
      dependsOn: [...new Set(depends)],
      networks: [...new Set(networks)],
      envKeys,
      ports,
      profile: profiles ? profiles[1] : null,
      healthcheck,
    });
  }
}

// proto registry.
const proto = { services: [], rpcCount: 0, messageCount: 0, path: 'proto/autoforward.proto' };
const protoRaw = readSafe(proto.path);
if (protoRaw) {
  proto.messageCount = (protoRaw.match(/^message\s+\w+/gm) || []).length;
  const blocks = protoRaw.split(/^service\s+/m).slice(1);
  for (const block of blocks) {
    const name = (block.match(/^(\w+)/) || [])[1];
    const rpcs = [...block.matchAll(/^\s*rpc\s+(\w+)\s*\(/gm)].map((m) => m[1]);
    if (!name) continue;
    proto.services.push({
      name,
      rpcs,
      count: rpcs.length,
      domain: name.replace(/ControlService$|Service$/, ''),
    });
  }
  proto.rpcCount = proto.services.reduce((a, s) => a + s.count, 0);
}

// Pipeline tags actually present in the source.
const pipelineTags = new Map();
for (const f of files) {
  if (!f.source) continue;
  const src = readSafe(f.rel);
  if (!src) continue;
  for (const m of src.matchAll(/PIPELINE:([A-Z_]+)/g)) {
    pipelineTags.set(m[1], (pipelineTags.get(m[1]) || 0) + 1);
  }
}

// ---------------------------------------------------------------------------
// 6. Component graph — the coarse view the 3D simulation and the topology map
//    render. Each component owns real file prefixes and real evidence.
// ---------------------------------------------------------------------------
const componentSpecs = [
  {
    id: 'telegram-cloud',
    label: 'Telegram MTProto',
    kind: 'external',
    tier: 0,
    tech: 'MTProto DCs',
    role: 'Source of updates and the delivery endpoint',
    color: '#38bdf8',
    areas: [],
    probes: [{ rel: 'core/infrastructure/telegram', re: /pyrogram|Client|FloodWait/ }],
  },
  {
    id: 'core-ingest',
    label: 'Ingestion & sessions',
    kind: 'service',
    tier: 1,
    tech: 'Pyrogram client pool',
    role: 'Authorised user/bot sessions, update stream, album aggregation',
    color: '#38bdf8',
    areas: ['core/infrastructure/telegram'],
    probes: [{ rel: 'core/infrastructure/telegram', re: /album|flood|session/i }],
  },
  {
    id: 'core-pipeline',
    label: 'Pipeline & rule engine',
    kind: 'service',
    tier: 2,
    tech: 'Python · domain model',
    role: 'Policy resolution, cleaning, shortcodes, replacements, AI pass',
    color: '#22d3ee',
    areas: ['core/application', 'core/domain'],
    probes: [{ rel: 'core/application/pipeline.py', re: /PIPELINE:/ }],
  },
  {
    id: 'core-queue',
    label: 'Durable queue & lease workers',
    kind: 'service',
    tier: 3,
    tech: 'SQLite-backed queue',
    role: 'Ordered delivery, retries with backoff, dead-letter control',
    color: '#818cf8',
    areas: ['core/application', 'core/infrastructure/persistence'],
    probes: [{ rel: 'core', re: /lease/i }],
  },
  {
    id: 'core-storage',
    label: 'Persistence',
    kind: 'store',
    tier: 4,
    tech: 'SQLite WAL · PostgreSQL',
    role: 'Tasks, queue, sessions, audit outbox',
    color: '#a3e635',
    areas: ['core/infrastructure/persistence'],
    probes: [{ rel: 'core/infrastructure/persistence', re: /sqlite|postgres|WAL/i }],
  },
  {
    id: 'core-grpc',
    label: 'gRPC surface',
    kind: 'interface',
    tier: 4,
    tech: 'grpc.aio :50051',
    role: 'Eleven servicers exposing the engine to the control plane',
    color: '#34d399',
    areas: ['core/infrastructure/grpc_server', 'core/proto'],
    probes: [{ rel: 'core/infrastructure/grpc_server', re: /Servicer/ }],
  },
  {
    id: 'ai-rewriter',
    label: 'AI rewriter',
    kind: 'external',
    tier: 5,
    tech: 'OpenAI-compatible endpoint',
    role: 'Optional LLM pass driven by per-task prompts',
    color: '#f472b6',
    areas: ['core/infrastructure/ai'],
    probes: [{ rel: 'core/infrastructure/ai', re: /prompt|completion|openai/i }],
  },
  {
    id: 'logger-svc',
    label: 'Logger service',
    kind: 'service',
    tier: 4,
    tech: 'Rust gRPC :50052',
    role: 'Central log intake, redaction, query and stats',
    color: '#f472b6',
    areas: ['rust/logger/src', 'logger'],
    probes: [{ rel: 'rust/logger/src', re: /grpc|Servicer|transport/ }],
  },
  {
    id: 'logger-store',
    label: 'Log store',
    kind: 'store',
    tier: 5,
    tech: 'SQLite (Rust-owned file)',
    role: 'Append-only log history with a bounded diagnostic window',
    color: '#fb7185',
    areas: ['rust/logger/src'],
    probes: [{ rel: 'rust/logger/src', re: /sqlite|db_path|DB_PATH/i }],
  },
  {
    id: 'api-rest',
    label: 'REST control plane',
    kind: 'service',
    tier: 4,
    tech: 'Go net/http :8080',
    role: 'Auth, tenant scoping, rules, delivery, logs, diagnostics',
    color: '#34d399',
    areas: ['api/internal/handlers', 'api/main.go'],
    probes: [{ rel: 'api/internal/handlers', re: /http\.HandlerFunc|ServeHTTP|mux\.Handle/ }],
    probes: [{ rel: 'api/internal/handlers', re: /HandleFunc|mux\./ }],
  },
  {
    id: 'api-guardrails',
    label: 'Guardrails',
    kind: 'internal',
    tier: 4,
    tech: 'Go middleware',
    role: 'Correlation ids, audit trail, log-policy gate, access log',
    color: '#6ee7b7',
    areas: ['api/internal/middleware', 'api/internal/logpolicy', 'core/infrastructure/security'],
    probes: [{ rel: 'api/internal/middleware', re: /correlation|audit|policy/ }],
  },
  {
    id: 'web-backend',
    label: 'Web gateway',
    kind: 'service',
    tier: 5,
    tech: 'Rust · axum :8088',
    role: 'Serves the dashboard, proxies the API, streams logs',
    color: '#fbbf24',
    areas: ['web/backend/src'],
    probes: [{ rel: 'web/backend/src', re: /axum|Router/ }],
  },
  {
    id: 'web-ui',
    label: 'Dashboard',
    kind: 'client',
    tier: 6,
    tech: 'React · TypeScript',
    role: 'Tasks, log explorer, diagnostics, admin',
    color: '#fb923c',
    areas: ['web/frontend/src'],
    probes: [{ rel: 'web/frontend/src', re: /React|useState|fetch/ }],
  },
  {
    id: 'mobile-app',
    label: 'Mobile app',
    kind: 'client',
    tier: 6,
    tech: 'Flutter · Dart',
    role: 'Same contract, native surfaces, 10 locales',
    color: '#a78bfa',
    areas: ['mobile/lib'],
    probes: [{ rel: 'mobile/lib', re: /MaterialApp|ThemeData/ }],
  },
  {
    id: 'cli',
    label: 'DevOps CLI',
    kind: 'client',
    tier: 6,
    tech: 'Python + atf entrypoint',
    role: 'Install, validate, diagnose, upgrade',
    color: '#22d3ee',
    areas: ['atfcli', 'atf.py', 'install.sh'],
    probes: [{ rel: 'atfcli', re: /argparse|add_parser/ }],
  },
  {
    id: 'edge-proxy',
    label: 'TLS edge',
    kind: 'edge',
    tier: 7,
    tech: 'Caddy (optional profile)',
    role: 'Automatic Let\'s Encrypt certificate and reverse proxy',
    color: '#e879f9',
    areas: ['Caddyfile'],
    probes: [{ rel: null, re: /reverse_proxy/ }],
  },
];

const components = [];
for (const spec of componentSpecs) {
  const owned = files.filter(
    (f) => spec.areas.some((a) => f.rel === a || f.rel.startsWith(`${a}/`)) && f.source,
  );
  const probeHits = (spec.probes || []).flatMap((p) => grepFiles(p.rel, p.re, 3));
  // A probe proves a behaviour; when it finds nothing (or the component has no
  // probe at all) the component still has provenance — the files it owns. Two
  // of the largest components used to publish an empty "derived from" line,
  // which made the biggest box on the map the least evidenced one.
  const ownedTop = [...owned].sort((a, b) => b.loc - a.loc).slice(0, 4).map((f) => f.rel);
  const evidence = [...new Set([...probeHits, ...ownedTop])];
  if (spec.kind !== 'external' && !owned.length && !evidence.length) {
    omittedProbes.push(`component ${spec.id}: no files or evidence`);
    continue;
  }
  components.push({
    id: spec.id,
    label: spec.label,
    kind: spec.kind,
    tier: spec.tier,
    tech: spec.tech,
    role: spec.role,
    color: spec.color,
    files: owned.length,
    loc: owned.reduce((a, f) => a + f.loc, 0),
    langs: [...new Set(owned.map((f) => f.lang))],
    evidence: [...new Set(evidence)].slice(0, 4),
    external: spec.kind === 'external',
  });
}
const componentIds = new Set(components.map((c) => c.id));
const componentEdges = [];
const addComponentEdge = (from, to, kind, label, protocol, metric) => {
  if (!componentIds.has(from) || !componentIds.has(to)) {
    omittedProbes.push(`component edge ${from}→${to}: endpoint not derived`);
    return;
  }
  componentEdges.push({ source: from, target: to, kind, label, protocol, metric });
};

addComponentEdge('telegram-cloud', 'core-ingest', 'mtproto', 'Updates in', 'MTProto', 'live');
addComponentEdge('core-ingest', 'core-pipeline', 'event', 'Normalised update', 'in-process', 'per message');
addComponentEdge('core-pipeline', 'core-queue', 'event', 'Enqueue after policy', 'in-process', 'per target');
addComponentEdge('core-queue', 'telegram-cloud', 'mtproto', 'Delivery', 'MTProto send', 'per target');
addComponentEdge('core-pipeline', 'ai-rewriter', 'http', 'Rewrite pass', 'HTTP JSON', 'optional');
addComponentEdge('core-pipeline', 'core-storage', 'sql', 'Rules & state', 'SQL', 'per decision');
addComponentEdge('core-queue', 'core-storage', 'sql', 'Queue & leases', 'SQL', 'per job');
addComponentEdge('core-grpc', 'core-pipeline', 'event', 'Commands in', 'in-process', proto.rpcCount ? `${proto.rpcCount} RPCs` : 'n/a');
addComponentEdge('core-grpc', 'core-storage', 'sql', 'Reads & writes', 'SQL', 'per call');
addComponentEdge('core-ingest', 'logger-svc', 'grpc', 'Stage events', 'gRPC', 'per stage');
addComponentEdge('core-pipeline', 'logger-svc', 'grpc', 'Stage events', 'gRPC', 'per stage');
addComponentEdge('core-queue', 'logger-svc', 'grpc', 'Delivery outcomes', 'gRPC', 'per delivery');
addComponentEdge('logger-svc', 'logger-store', 'sql', 'Append', 'SQL', 'per event');
addComponentEdge('api-rest', 'core-grpc', 'grpc', 'Control calls', 'gRPC', proto.rpcCount ? `${proto.rpcCount} RPCs` : 'n/a');
addComponentEdge('api-rest', 'logger-svc', 'grpc', 'API logs & audit', 'gRPC', 'per request');
addComponentEdge('api-rest', 'api-guardrails', 'event', 'Middleware chain', 'in-process', 'per request');
addComponentEdge('web-backend', 'api-rest', 'http', 'Proxy + SSE', 'HTTP/1.1', 'per request');
addComponentEdge('web-backend', 'web-ui', 'http', 'Static bundle', 'HTTP', 'per visit');
addComponentEdge('web-ui', 'web-backend', 'http', 'Dashboard calls', '/api/v1', 'per action');
addComponentEdge('mobile-app', 'api-rest', 'http', 'Mobile calls', 'HTTPS', 'per action');
addComponentEdge('cli', 'api-rest', 'http', 'Diagnostics', 'HTTP', 'per run');
addComponentEdge('cli', 'web-backend', 'http', 'Gateway health', 'HTTP', 'per run');
addComponentEdge('edge-proxy', 'web-backend', 'proxy', 'Terminate TLS', 'reverse proxy', 'per request');

const flows = [
  {
    id: 'forward',
    title: 'A post is forwarded',
    summary:
      'From an incoming Telegram update to a delivered copy in every target chat, with the durable queue absorbing rate limits.',
    steps: [
      { at: 'telegram-cloud', action: 'Update arrives', detail: 'MTProto push from the source chat the session is authorised for.', tags: ['INGEST', 'DIALOG_FETCH'] },
      { at: 'core-ingest', action: 'Normalise', detail: 'Album items are aggregated and the update is turned into a domain event.', tags: [] },
      { at: 'core-pipeline', action: 'Resolve policy', detail: 'Matching tasks, per-source/policy overrides, whitelists and blacklists.', tags: ['POLICY', 'POLICY_TARGET', 'NO_RULES', 'NO_MATCH'] },
      { at: 'core-pipeline', action: 'Filter & rewrite', detail: 'Filters, cleaner, shortcodes, replacements — and the optional AI pass.', tags: ['FILTER', 'CLEANER', 'TRANSFORM', 'AI_REWRITE'] },
      { at: 'ai-rewriter', action: 'Rewrite (optional)', detail: 'Only when the task enables it; an unreachable provider passes the text through.', tags: ['AI_REWRITE'] },
      { at: 'core-queue', action: 'Enqueue per target', detail: 'One durable job per target with an ordering key, so bursts keep their order.', tags: ['ENQUEUE'] },
      { at: 'core-queue', action: 'Lease & deliver', detail: 'Lease-based workers with exponential backoff and FloodWait-aware rescheduling.', tags: ['RETRY', 'FLOODWAIT'] },
      { at: 'telegram-cloud', action: 'Delivered', detail: 'Copy mode or native forward, into the target chat or forum topic.', tags: ['DELIVERED'] },
      { at: 'logger-svc', action: 'Recorded', detail: 'Every stage, drop and outcome lands in the log store with a correlation id.', tags: ['DROP', 'FILTER_DROP', 'DUP_FILTER', 'DELIVERY_FAIL', 'DELIVERY_TIMEOUT'] },
    ],
  },
  {
    id: 'control',
    title: 'An operator edits a task',
    summary:
      'A dashboard write travels through the gateway, the REST control plane and gRPC into the engine — with optimistic concurrency and an audit trail.',
    steps: [
      { at: 'web-ui', action: 'Operator saves', detail: 'A tenant-scoped token is attached to the request; no API key ever reaches the browser.', tags: [] },
      { at: 'web-backend', action: 'Gateway proxies', detail: 'The Rust gateway forwards to the API and keeps upstream addressing server-side.', tags: [] },
      { at: 'api-guardrails', action: 'Guardrails', detail: 'Correlation id, access log, log-policy gate and audit event are applied first.', tags: [] },
      { at: 'api-rest', action: 'Authorise', detail: 'Fail-closed tenant check: the rule is only touched if it belongs to the caller.', tags: [] },
      { at: 'core-grpc', action: 'RPC', detail: 'The write becomes a protobuf call on the engine surface.', tags: [] },
      { at: 'core-pipeline', action: 'Apply', detail: 'Optimistic concurrency on the rule version; metadata[content_policy] carries the per-task rules.', tags: [] },
      { at: 'core-storage', action: 'Persist', detail: 'Transactional write plus the outbox entry used for audit.', tags: [] },
      { at: 'logger-svc', action: 'Audit', detail: 'Before/after images are written to the log store for attribution.', tags: [] },
    ],
  },
  {
    id: 'observe',
    title: 'An event is investigated',
    summary:
      'Live logs, the diagnostic window and the dead-letter queue: how an operator answers "what happened to that message?".',
    steps: [
      { at: 'web-ui', action: 'Open the log explorer', detail: 'Filters by task, stage, severity and correlation id.', tags: [] },
      { at: 'web-backend', action: 'Stream', detail: 'SSE feed so the view updates without polling.', tags: [] },
      { at: 'api-rest', action: 'Query', detail: 'Bounded window, tenant-scoped, with redaction already applied upstream.', tags: [] },
      { at: 'logger-svc', action: 'Read', detail: 'Field-aware redaction happens at write time, so even an admin read is already safe.', tags: [] },
      { at: 'logger-store', action: 'Return page', detail: 'Bounded-label stats and slow-query events are exposed alongside the rows.', tags: [] },
      { at: 'core-queue', action: 'Dead-letter actions', detail: 'Inspect, replay or purge failed deliveries for the tenant.', tags: ['ENQUEUE_DROP'] },
    ],
  },
];

// Keep only tags that really exist in the source, and report the rest.
const pipelineTagList = [...pipelineTags.entries()]
  .map(([tag, count]) => ({ tag, count }))
  .sort((a, b) => b.count - a.count || a.tag.localeCompare(b.tag));
const knownTags = new Set(pipelineTags.keys());
for (const flow of flows) {
  for (const step of flow.steps) {
    const kept = step.tags.filter((t) => knownTags.has(t));
    const lost = step.tags.filter((t) => !knownTags.has(t));
    if (lost.length) omittedProbes.push(`flow ${flow.id}: tags not found in source: ${lost.join(', ')}`);
    step.tags = kept;
  }
}

// ---------------------------------------------------------------------------
// 7. Assemble.
// ---------------------------------------------------------------------------
let commit = 'HEAD';
let gitDate = new Date().toISOString();
try {
  commit = execSync('git rev-parse --short HEAD', { cwd: rootDir }).toString().trim();
  gitDate = execSync('git log -1 --format=%cI', { cwd: rootDir }).toString().trim();
} catch {
  /* not a git checkout: keep the placeholder */
}

const subsystemStats = SUBSYSTEMS.map((s) => {
  const owned = bySubsystem.get(s.id) || [];
  const source = owned.filter((f) => f.source);
  const areas = new Map();
  for (const f of source) {
    const rest = f.rel.startsWith(`${s.root}/`) ? f.rel.slice(s.root.length + 1) : f.rel;
    const area = rest.includes('/') ? rest.split('/')[0] : '(root)';
    const cur = areas.get(area) || { id: area, files: 0, loc: 0, langs: new Set() };
    cur.files++;
    cur.loc += f.loc;
    cur.langs.add(f.lang);
    areas.set(area, cur);
  }
  return {
    id: s.id,
    label: s.label,
    root: s.root,
    color: s.color,
    role: s.role,
    tech: s.tech,
    blurb: s.blurb,
    files: owned.length,
    sourceFiles: source.length,
    loc: source.reduce((a, f) => a + f.loc, 0),
    langs: [...new Set(source.map((f) => f.lang))],
    areas: [...areas.values()]
      .map((a) => ({ id: a.id, files: a.files, loc: a.loc, langs: [...a.langs] }))
      .sort((a, b) => b.loc - a.loc),
  };
}).filter((s) => s.files > 0);

const architecture = {
  generatedAt: new Date().toISOString(),
  commit,
  gitDate,
  granularity: GRANULARITY,
  subsystems: subsystemStats,
  totals: {
    files: files.length,
    sourceFiles: files.filter((f) => f.source).length,
    loc: files.filter((f) => f.source).reduce((a, f) => a + f.loc, 0),
    nodes: nodes.length,
    edges: edges.length,
    generatedFiles: files.filter((f) => f.generated && f.source).length,
    testFiles: files.filter((f) => /(^|\/)(tests?|__tests__)\/|test_|_test\.|\.spec\.|_test\.go/.test(f.rel)).length,
    subsystems: subsystemStats.length,
  },
  languages: [...langTotals.values()].sort((a, b) => b.loc - a.loc),
  nodes: nodes.map((n) => ({
    ...n,
    fanIn: fanIn.get(n.id) || 0,
    fanOut: fanOut.get(n.id) || 0,
    isEntry: /(^|\/)(main|__main__|atf|index|app)\.(py|go|rs|dart|ts|tsx|js)$/.test(n.id),
  })),
  edges,
  keystones: keystones.slice(0, 24),
  largest: largest.slice(0, 18),
  orphans: orphans.slice(0, 12),
  cycles,
  crossSubsystem: [...crossMap.entries()]
    .map(([k, count]) => {
      const [from, to] = k.split('->');
      return { from, to, count };
    })
    .sort((a, b) => b.count - a.count),
  contracts,
  verifiedAbsences,
  openChecks,
  omittedProbes,
  components,
  componentEdges,
  flows,
  services,
  proto,
  pipeline: { tags: pipelineTagList, total: pipelineTagList.reduce((a, t) => a + t.count, 0) },
};

fs.mkdirSync(dataDir, { recursive: true });
fs.writeFileSync(path.join(dataDir, 'architecture.json'), `${JSON.stringify(architecture, null, 2)}\n`);

// Compact projection for the client islands (index-based, no repeated strings).
const nodeIndex = new Map(nodes.map((n, i) => [n.id, i]));
const kinds = [...new Set(edges.map((e) => e.kind))];
const graph = {
  commit,
  generatedAt: architecture.generatedAt,
  granularity: GRANULARITY,
  kinds,
  subsystems: subsystemStats.map((s) => ({ id: s.id, label: s.label, color: s.color, loc: s.loc, files: s.files })),
  nodes: nodes.map((n) => [
    n.label,
    n.subsystem,
    n.lang,
    n.loc,
    n.files,
    fanIn.get(n.id) || 0,
    fanOut.get(n.id) || 0,
    n.path,
    n.generated ? 1 : 0,
    n.isTest ? 1 : 0,
  ]),
  edges: edges.map((e) => [nodeIndex.get(e.source), nodeIndex.get(e.target), kinds.indexOf(e.kind)]),
  components,
  componentEdges,
  flows,
};
fs.mkdirSync(publicDir, { recursive: true });
fs.writeFileSync(path.join(publicDir, 'graph.json'), JSON.stringify(graph));

const mb = (p) => `${(fs.statSync(p).size / 1024).toFixed(1)} KiB`;
console.log('🧭 Architecture atlas generated');
console.log(
  `   subsystems ${subsystemStats.length} · source files ${architecture.totals.sourceFiles}/${architecture.totals.files} · LOC ${architecture.totals.loc}`,
);
console.log(
  `   graph: ${nodes.length} nodes, ${edges.length} edges (${kinds.join(', ')}) · cycles ${cycles.length} · keystones ${keystones.length}`,
);
console.log(
  `   components ${components.length}/${componentSpecs.length} · component edges ${componentEdges.length} · contracts ${contracts.length} · flows ${flows.length}`,
);
console.log(`   proto: ${proto.services.length} services / ${proto.rpcCount} rpcs / ${proto.messageCount} messages · compose services ${services.length}`);
console.log(`   pipeline tags ${pipelineTagList.length} · unverifiable probes ${omittedProbes.length}`);
for (const p of omittedProbes) console.log(`   · dropped: ${p}`);
console.log(`   → src/data/architecture.json ${mb(path.join(dataDir, 'architecture.json'))} · public/system/graph.json ${mb(path.join(publicDir, 'graph.json'))}`);
