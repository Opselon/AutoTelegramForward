import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * System-atlas gate.
 *
 * The /system/ pages claim things about this repository that no other test can
 * see. This spec checks the derived data for internal consistency (an edge that
 * points at a node that does not exist is a lie drawn as a line), then checks
 * that the artifact actually shipped: the graph the islands fetch, the three
 * routes, the sitemap, the search index and llms.txt.
 *
 * A failure here means the map and the repository disagree — which is exactly
 * the drift a hand-maintained architecture diagram always ends up with.
 */

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, '../');
const dataPath = path.join(root, 'src/data/architecture.json');
const graphPath = path.join(root, 'public/system/graph.json');
const distDir = path.join(root, 'dist');

let errors = 0;
let checks = 0;
const fail = (message) => {
  console.error(`❌ ${message}`);
  errors++;
};
const ok = (message) => {
  checks++;
  if (process.env.ATLAS_VERBOSE) console.log(`✅ ${message}`);
};

if (!fs.existsSync(dataPath)) {
  console.error('❌ src/data/architecture.json is missing — run `npm run sync` before the audits.');
  process.exit(1);
}
if (!fs.existsSync(graphPath)) {
  console.error('❌ public/system/graph.json is missing — run `npm run sync` before the audits.');
  process.exit(1);
}

const arch = JSON.parse(fs.readFileSync(dataPath, 'utf8'));
const graph = JSON.parse(fs.readFileSync(graphPath, 'utf8'));

// ---------------------------------------------------------------------------
// 1. The derived repository graph must be internally consistent.
// ---------------------------------------------------------------------------
const nodeIds = new Set(arch.nodes.map((n) => n.id));
if (nodeIds.size !== arch.nodes.length) fail('architecture.json has duplicate node ids');
ok(`${arch.nodes.length} nodes, all unique`);

for (const edge of arch.edges) {
  if (!nodeIds.has(edge.source)) fail(`edge source ${edge.source} is not a node`);
  if (!nodeIds.has(edge.target)) fail(`edge target ${edge.target} is not a node`);
}
ok(`${arch.edges.length} import edges all resolve to nodes`);

const seen = new Set();
for (const edge of arch.edges) {
  const key = `${edge.source}\u0000${edge.target}\u0000${edge.kind}`;
  if (seen.has(key)) fail(`duplicate edge ${edge.source} → ${edge.target} (${edge.kind})`);
  seen.add(key);
}
ok('no duplicate edges');

if (arch.totals.nodes !== arch.nodes.length) fail('totals.nodes disagrees with the node list');
if (arch.totals.edges !== arch.edges.length) fail('totals.edges disagrees with the edge list');
if (arch.nodes.length < 100) fail(`only ${arch.nodes.length} modules derived — the sweep looks broken`);
if (arch.totals.loc < 10000) fail(`only ${arch.totals.loc} lines derived — the sweep looks broken`);
ok(`${arch.totals.loc} lines across ${arch.totals.sourceFiles} source files`);

for (const subsystem of arch.subsystems) {
  if (!subsystem.sourceFiles) fail(`subsystem ${subsystem.id} has no source files`);
  if (!subsystem.loc) fail(`subsystem ${subsystem.id} has no lines counted`);
}
ok(`${arch.subsystems.length} subsystems all carry files and lines`);

for (const language of arch.languages) {
  if (!language.files || !language.loc) fail(`language ${language.lang} has an empty count`);
}
ok(`${arch.languages.length} languages counted`);

// Keystones: a ranking that is not sorted is a ranking nobody can trust.
for (let i = 1; i < arch.keystones.length; i++) {
  if (arch.keystones[i - 1].score < arch.keystones[i].score) fail('keystones are not sorted by score');
}
for (const entry of arch.keystones) {
  if (entry.fanIn + entry.fanOut === 0) fail(`keystone ${entry.id} has no links at all`);
}
for (const entry of arch.largest) {
  if (entry.fanIn + entry.fanOut === 0) fail(`largest module ${entry.id} has no links at all`);
}
if (arch.keystones.some((k) => /(test_|_test\.|\.spec\.)/.test(k.id))) fail('tests are ranked as keystones');
if (arch.largest.some((k) => /(test_|_test\.|\.spec\.)/.test(k.id))) fail('tests are ranked as largest modules');
ok(`keystones ${arch.keystones.length} and largest ${arch.largest.length} ranked without tests`);

for (const cycle of arch.cycles) {
  if (cycle.length < 2) fail(`cycle reported with ${cycle.length} module(s): ${cycle.join(', ')}`);
}
ok(`${arch.cycles.length} cycles, each with at least two members`);

// ---------------------------------------------------------------------------
// 2. Runtime components: every drawn connection needs two real endpoints and
//    every flow step needs a component to land on.
// ---------------------------------------------------------------------------
const componentIds = new Set(arch.components.map((c) => c.id));
if (componentIds.size !== arch.components.length) fail('duplicate component ids');
if (arch.components.length < 8) fail(`only ${arch.components.length} runtime components derived`);
for (const edge of arch.componentEdges) {
  if (!componentIds.has(edge.source)) fail(`component edge source ${edge.source} does not exist`);
  if (!componentIds.has(edge.target)) fail(`component edge target ${edge.target} does not exist`);
  if (!edge.protocol) fail(`component edge ${edge.source}→${edge.target} has no protocol`);
}
ok(`${arch.components.length} components and ${arch.componentEdges.length} component links`);

const tagSet = new Set(arch.pipeline.tags.map((t) => t.tag));
if (tagSet.size === 0) fail('no pipeline tags were parsed out of the source');
if (arch.flows.length === 0) fail('no runtime flows derived');
for (const flow of arch.flows) {
  if (flow.steps.length < 3) fail(`flow ${flow.id} has only ${flow.steps.length} steps`);
  for (const step of flow.steps) {
    if (!componentIds.has(step.at)) fail(`flow ${flow.id} step targets unknown component ${step.at}`);
    if (!step.action || !step.detail) fail(`flow ${flow.id} has an empty step`);
    for (const tag of step.tags) {
      if (!tagSet.has(tag)) fail(`flow ${flow.id} quotes PIPELINE:${tag}, which is not in the parsed tag list`);
    }
  }
}
ok(`${arch.flows.length} flows, ${arch.flows.reduce((a, f) => a + f.steps.length, 0)} steps, all tags verified against source`);

// ---------------------------------------------------------------------------
// 3. Claims: a published link must carry evidence, and a published absence must
//    name the scope it searched.
// ---------------------------------------------------------------------------
if (arch.contracts.length < 5) fail(`only ${arch.contracts.length} inter-service contracts derived`);
for (const contract of arch.contracts) {
  if (!contract.evidence || contract.evidence.length === 0) {
    fail(`contract ${contract.from}→${contract.to} is published without evidence`);
  }
  for (const file of contract.evidence || []) {
    if (!fs.existsSync(path.join(root, '..', file))) fail(`contract evidence ${file} does not exist in the checkout`);
  }
}
ok(`${arch.contracts.length} contracts, each with an existing evidence file`);

for (const absence of arch.verifiedAbsences) {
  if (!absence.scope) fail(`verified absence "${absence.label}" does not record what it searched`);
  if (!absence.claim) fail(`verified absence "${absence.label}" has no claim text`);
}
for (const check of arch.openChecks) {
  if (!check.hits || check.hits.length === 0) fail(`open check "${check.label}" was recorded without hits`);
}
ok(`${arch.verifiedAbsences.length} verified absences and ${arch.openChecks.length} open items`);

if (!arch.proto.services.length) fail('no proto services parsed');
const rpcSum = arch.proto.services.reduce((a, s) => a + s.count, 0);
if (rpcSum !== arch.proto.rpcCount) fail(`proto rpcCount ${arch.proto.rpcCount} disagrees with the per-service sum ${rpcSum}`);
for (const service of arch.proto.services) {
  if (!service.rpcs.length) fail(`proto service ${service.name} lists no methods`);
  if (service.count !== service.rpcs.length) fail(`proto service ${service.name} count disagrees with its method list`);
}
ok(`${arch.proto.services.length} proto services / ${arch.proto.rpcCount} rpcs / ${arch.proto.messageCount} messages`);

for (const service of arch.services) {
  if (!service.container) fail(`compose service ${service.name} has no container name`);
  for (const dependency of service.dependsOn) {
    if (!arch.services.some((s) => s.name === dependency)) {
      fail(`compose service ${service.name} waits for undeclared service ${dependency}`);
    }
    if (dependency === service.name) fail(`compose service ${service.name} waits for itself`);
  }
}
ok(`${arch.services.length} compose services, dependency graph closed`);

// ---------------------------------------------------------------------------
// 4. The client projection must agree with the build-time data.
// ---------------------------------------------------------------------------
if (graph.nodes.length !== arch.nodes.length) fail('graph.json node count disagrees with architecture.json');
if (graph.edges.length !== arch.edges.length) fail('graph.json edge count disagrees with architecture.json');
for (const [from, to] of graph.edges) {
  if (from === undefined || to === undefined || from < 0 || to < 0) fail('graph.json edge references a missing node index');
  if (from >= graph.nodes.length || to >= graph.nodes.length) fail('graph.json edge index is out of range');
  if (typeof from !== 'number' || typeof to !== 'number') fail('graph.json edge is not an index pair');
}
if (graph.nodes.some((row) => row.length !== 10)) fail('graph.json node rows must carry 10 fields each');
if (graph.components.length !== arch.components.length) fail('graph.json components disagree with architecture.json');
if (graph.componentEdges.length !== arch.componentEdges.length) fail('graph.json component edges disagree');
if (graph.flows.length !== arch.flows.length) fail('graph.json flows disagree with architecture.json');
ok(`graph.json projection consistent (${graph.nodes.length} nodes, ${graph.edges.length} edges)`);

// ---------------------------------------------------------------------------
// 5. What shipped. Skipped with a clear notice when dist/ is not present.
// ---------------------------------------------------------------------------
if (!fs.existsSync(distDir)) {
  console.warn('⚠️  dist/ not built — the shipped-artifact assertions were skipped. Build first.');
} else {
  const routes = ['system', 'system/dependencies', 'system/connections'];
  const islandRoutes = ['system/dependencies', 'system/connections'];
  for (const route of routes) {
    const file = path.join(distDir, route, 'index.html');
    if (!fs.existsSync(file)) {
      fail(`dist/${route}/index.html was not emitted`);
      continue;
    }
    const html = fs.readFileSync(file, 'utf8');
    if (islandRoutes.includes(route) && !html.includes('graph.json')) {
      fail(`${route} does not reference the graph payload`);
    }
    if (!/<h1[^>]*>/.test(html)) fail(`${route} has no h1`);
    if (islandRoutes.includes(route) && !html.includes('<canvas')) {
      fail(`${route} has no canvas — the interactive map did not render`);
    }
  }
  const hub = fs.readFileSync(path.join(distDir, 'system/index.html'), 'utf8');
  if (!hub.includes('Runtime topology')) fail('the atlas hub does not render the topology map');
  for (const link of ['/system/dependencies/', '/system/connections/']) {
    if (!hub.includes(link)) fail(`the atlas hub does not link to ${link}`);
  }
  ok('all three atlas routes emitted, with the hub linking to both deep maps');

  const shippedGraph = path.join(distDir, 'system/graph.json');
  if (!fs.existsSync(shippedGraph)) fail('dist/system/graph.json was not shipped — the islands would 404');
  else {
    const shipped = JSON.parse(fs.readFileSync(shippedGraph, 'utf8'));
    if (shipped.nodes.length !== arch.nodes.length) fail('the shipped graph.json is stale');
    ok('the shipped graph.json matches the derived data');
  }

  const sitemap = path.join(distDir, 'sitemap-0.xml');
  if (fs.existsSync(sitemap)) {
    const xml = fs.readFileSync(sitemap, 'utf8');
    for (const route of routes) {
      if (!xml.includes(`/${route}/`)) fail(`sitemap is missing /${route}/`);
    }
    ok('sitemap carries every atlas route');
  } else {
    fail('dist/sitemap-0.xml is missing');
  }

  const searchFile = path.join(distDir, 'search.json');
  if (fs.existsSync(searchFile)) {
    const entries = JSON.parse(fs.readFileSync(searchFile, 'utf8'));
    for (const route of routes) {
      if (!entries.some((entry) => entry.url.includes(`/${route}/`))) {
        fail(`search.json does not index /${route}/ — the page is unreachable from the search overlay`);
      }
    }
    ok(`search.json indexes the atlas (${entries.length} entries)`);
  } else {
    fail('dist/search.json is missing');
  }

  const llms = path.join(distDir, 'llms.txt');
  if (fs.existsSync(llms) && !fs.readFileSync(llms, 'utf8').includes('/system/')) {
    fail('llms.txt does not mention the system atlas');
  } else if (fs.existsSync(llms)) {
    ok('llms.txt lists the atlas for answer engines');
  }
}

// ---------------------------------------------------------------------------
// 6. The hook must run inside the build, or the pages would silently describe
//    whatever was generated last.
// ---------------------------------------------------------------------------
const pkg = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
if (!String(pkg.scripts.build).includes('sync-architecture.mjs')) {
  fail('npm run build does not run scripts/sync-architecture.mjs');
}
if (!String(pkg.scripts.test).includes('test:system')) {
  fail('npm test does not run the system-atlas gate');
}
ok('build and test pipelines both run the atlas generator and this gate');

console.log(
  `\n🧭 System atlas: ${arch.subsystems.length} subsystems · ${arch.nodes.length} modules · ${arch.edges.length} edges · ` +
    `${arch.components.length} components · ${arch.componentEdges.length} links · ${arch.cycles.length} cycles · ` +
    `${arch.verifiedAbsences.length} verified absences · ${arch.pipeline.tags.length} pipeline tags`,
);
console.log(`   ${checks} assertions passed`);

if (errors) {
  console.error(`\n❌ System atlas audit failed with ${errors} error(s)!`);
  process.exit(1);
}
console.log('✅ System atlas audit passed.');
