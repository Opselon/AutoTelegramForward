/**
 * Selectors and formatters over architecture.json — the file the build-time
 * sweep in scripts/sync-architecture.mjs derives from this checkout.
 *
 * Pages must never invent a number: everything the /system/ pages state comes
 * from here, so a claim and its evidence cannot drift apart.
 */
import arch from './architecture.json';
import { repoInfo } from './site';
import { url } from '../utils/paths';

export interface Subsystem {
  id: string;
  label: string;
  root: string;
  color: string;
  role: string;
  tech: string;
  blurb: string;
  files: number;
  sourceFiles: number;
  loc: number;
  langs: string[];
  areas: Array<{ id: string; files: number; loc: number; langs: string[] }>;
}

export interface Component {
  id: string;
  label: string;
  kind: string;
  tier: number;
  tech: string;
  role: string;
  color: string;
  files: number;
  loc: number;
  langs: string[];
  evidence: string[];
  external: boolean;
}

export interface ComponentEdge {
  source: string;
  target: string;
  kind: string;
  label: string;
  protocol: string;
  metric: string;
}

export interface FlowStep {
  at: string;
  action: string;
  detail: string;
  tags: string[];
}

export interface Flow {
  id: string;
  title: string;
  summary: string;
  steps: FlowStep[];
}

export interface Contract {
  from: string;
  to: string;
  kind: string;
  label: string;
  protocol: string;
  detail: string;
  evidence: string[];
}

export interface NodeRow {
  id: string;
  label: string;
  path: string;
  subsystem: string;
  granularity: 'file' | 'package';
  granularityLabel: string;
  lang: string;
  files: number;
  loc: number;
  generated: boolean;
  isTest: boolean;
  fanIn: number;
  fanOut: number;
  areas: string[];
  isEntry: boolean;
}

export const atlas = arch as unknown as {
  generatedAt: string;
  commit: string;
  gitDate: string;
  granularity: Record<string, string>;
  subsystems: Subsystem[];
  totals: {
    files: number;
    sourceFiles: number;
    loc: number;
    nodes: number;
    edges: number;
    generatedFiles: number;
    testFiles: number;
    subsystems: number;
  };
  languages: Array<{ lang: string; files: number; loc: number; bytes: number }>;
  nodes: NodeRow[];
  edges: Array<{ source: string; target: string; kind: string }>;
  keystones: Array<{
    id: string;
    label: string;
    subsystem: string;
    lang: string;
    loc: number;
    fanIn: number;
    fanOut: number;
    score: number;
  }>;
  largest: Array<{
    id: string;
    label: string;
    subsystem: string;
    lang: string;
    files: number;
    loc: number;
    fanIn: number;
    fanOut: number;
  }>;
  orphans: Array<{ id: string; label: string; subsystem: string; loc: number }>;
  cycles: string[][];
  crossSubsystem: Array<{ from: string; to: string; count: number }>;
  contracts: Contract[];
  verifiedAbsences: Array<{ label: string; claim: string; scope: string }>;
  openChecks: Array<{ label: string; claim: string; hits: string[] }>;
  omittedProbes: string[];
  components: Component[];
  componentEdges: ComponentEdge[];
  flows: Flow[];
  services: Array<{
    name: string;
    container: string;
    dockerfile: string | null;
    image: string | null;
    dependsOn: string[];
    networks: string[];
    envKeys: string[];
    ports: string[];
    profile: string | null;
    healthcheck: boolean;
  }>;
  proto: {
    path: string;
    services: Array<{ name: string; rpcs: string[]; count: number; domain: string }>;
    rpcCount: number;
    messageCount: number;
  };
  pipeline: { tags: Array<{ tag: string; count: number }>; total: number };
};

export const subsystemById = new Map(atlas.subsystems.map((s) => [s.id, s]));
export const componentById = new Map(atlas.components.map((c) => [c.id, c]));

export const subsystemColor = (id: string) => subsystemById.get(id)?.color || '#94a3b8';

/** Kind → colour, used by the topology map, the 3D scene and the legends. */
export const edgeKindColor: Record<string, string> = {
  mtproto: '#38bdf8',
  event: '#a78bfa',
  grpc: '#34d399',
  http: '#fbbf24',
  sql: '#a3e635',
  proxy: '#e879f9',
  codegen: '#c084fc',
  build: '#94a3b8',
  import: '#64748b',
};

export const componentKindLabel: Record<string, string> = {
  external: 'External system',
  service: 'Service',
  internal: 'Internal module',
  store: 'Data store',
  interface: 'Interface',
  client: 'Client',
  edge: 'Edge proxy',
};

/** GitHub blob link for a derived path — every map node is one click from source. */
export const sourceUrl = (relPath: string) =>
  `${repoInfo.repoUrl}/blob/master/${relPath.replace(/^\//, '')}`;

export const graphUrl = url('/system/graph.json');

export const number = (value: number) => value.toLocaleString('en-US');

/** 102957 → 103k, for headline stats where the exact digits do not matter. */
export const compact = (value: number) =>
  value >= 10000 ? `${Math.round(value / 1000)}k` : value.toLocaleString('en-US');

export const bytes = (value: number) =>
  value > 1024 * 1024 ? `${(value / 1024 / 1024).toFixed(1)} MiB` : `${Math.round(value / 1024)} KiB`;

export const componentEdgesOf = (id: string) =>
  atlas.componentEdges.filter((e) => e.source === id || e.target === id);

export const contractsOf = (id: string) => atlas.contracts.filter((c) => c.from === id || c.to === id);

export { repoInfo, url };
