// Topology for the React Flow floorplan: wing backdrops, agent/storage/ops
// nodes, and the flow edges between them. Positions are hand-authored to match
// the mockup's wings (not auto-laid-out). Live data is injected at render time;
// this file is the static structure only.

import {
  Archive, Boxes, Calendar, Compass, Cpu, Database, FileText, FlaskConical, Github, Globe,
  Inbox, Microscope, Network, PenLine, ScrollText, ShieldCheck, Users, type LucideIcon,
} from "lucide-react";

export type FloorNodeType = "wing" | "scout" | "mimir" | "storage" | "ops" | "dormant" | "librarybox";
export type InspectorKind = "mimir" | "library" | "scout" | "gate" | "info";
export type Handle = "t" | "b" | "l" | "r";

export interface NodeDef {
  id: string;
  type: FloorNodeType;
  x: number;
  y: number;
  w: number;
  h: number;
  title?: string;
  sub?: string;
  icon?: LucideIcon;
  live?: boolean;
  sourceKind?: string; // scouts
  storageVariant?: "raw" | "vector" | "graph" | "claims" | "experiments"; // storage
  inspector?: InspectorKind; // what a click opens
  description?: string; // dormant rooms
  wingLabel?: string; // wing backdrops
}

export interface EdgeDef {
  id: string;
  source: string;
  target: string;
  sourceHandle: Handle;
  targetHandle: Handle;
  kind: "intake" | "knowledge" | "workflow";
  live: boolean;
  hotEvents: string[];
  sourceKind?: string; // scope heat to one scout kind
}

const SCOUT_EVENTS = ["source.discovered", "library.sweep_requested", "library.trends"];
const LIB_EVENTS = ["document.ingested", "document.parsed"];

// --- Wing backdrops (rendered behind, non-interactive) -------------------
const WINGS: NodeDef[] = [
  { id: "wing-collectors", type: "wing", x: 32, y: 40, w: 1000, h: 252, wingLabel: "Collectors Wing" },
  { id: "wing-research", type: "wing", x: 1064, y: 40, w: 588, h: 252, wingLabel: "Research & Discovery" },
  { id: "wing-knowledge", type: "wing", x: 32, y: 316, w: 1000, h: 432, wingLabel: "Knowledge Core" },
  { id: "wing-evaluation", type: "wing", x: 1064, y: 316, w: 588, h: 432, wingLabel: "Evaluation & Output" },
  { id: "wing-operations", type: "wing", x: 32, y: 772, w: 1620, h: 232, wingLabel: "Operations Wing" },
];

// --- Scouts (live) -------------------------------------------------------
const SCOUTS: NodeDef[] = [
  { id: "web", sourceKind: "web", title: "Web Scout", sub: "Open-web discovery", icon: Globe },
  { id: "arxiv", sourceKind: "arxiv", title: "arXiv Scout", sub: "Scientific papers", icon: FileText },
  { id: "github", sourceKind: "github", title: "GitHub Scout", sub: "Code repositories", icon: Github },
  { id: "openml", sourceKind: "openml", title: "OpenML Scout", sub: "Benchmarks & datasets", icon: Database },
  { id: "dataset", sourceKind: "dataset", title: "Dataset Scout", sub: "HF datasets hub", icon: Boxes },
].map((s, i) => ({
  ...s, type: "scout" as const, x: 56 + i * 192, y: 112, w: 178, h: 156, live: true, inspector: "scout" as const,
}));

// --- Research & Discovery (dormant) -------------------------------------
const RESEARCH: NodeDef[] = [
  { id: "ariadne", x: 1088, y: 112, w: 180, h: 156, title: "Ariadne", sub: "Principal Investigator", icon: Compass,
    description: "Frames research directions and decides what to pursue or kill. Activates with the research workflow." },
  { id: "planner", x: 1284, y: 112, w: 150, h: 156, title: "Planner", sub: "Schedules & goals", icon: Calendar,
    description: "Turns directions into concrete, falsifiable tasks. Activates with the research workflow." },
  { id: "researchers", x: 1450, y: 112, w: 186, h: 156, title: "Researchers", sub: "Investigate & gather", icon: Users,
    description: "Investigate directions and gather grounded evidence from the Library. Activates with the research workflow." },
].map((n) => ({ ...n, type: "dormant" as const, live: false, inspector: "info" as const }));

// --- Knowledge core: Mimir + request queue + storage row ----------------
const CORE: NodeDef[] = [
  { id: "mimir", type: "mimir", x: 72, y: 356, w: 372, h: 196, title: "Mimir", sub: "AI Curator of Knowledge",
    icon: ShieldCheck, live: true, inspector: "mimir" },
  { id: "request-queue", type: "dormant", x: 720, y: 356, w: 280, h: 196, title: "Request Queue", sub: "Acquire asks", icon: Inbox,
    live: false, inspector: "info",
    description: "Agents request missing sources here once the research workflow is live. Empty by design today." },
  // Decorative container that groups the three live storage cards as one Library
  // (they all open the same Library inspector). Anchors the Mimir -> Library flow.
  { id: "library-box", type: "librarybox", x: 44, y: 572, w: 582, h: 172, title: "Library", sub: "Queryable research memory", live: true },
];

const STORAGE_SRC: Array<Pick<NodeDef, "id" | "storageVariant" | "title" | "sub" | "icon" | "live" | "inspector" | "description">> = [
  { id: "raw-archive", storageVariant: "raw", title: "Raw Archive", sub: "Documents & files", icon: Archive, live: true, inspector: "library" },
  { id: "vector-memory", storageVariant: "vector", title: "Vector Memory", sub: "Embeddings & chunks", icon: Boxes, live: true, inspector: "library" },
  { id: "context-graph", storageVariant: "graph", title: "Context Graph", sub: "Nodes & relationships", icon: Network, live: true, inspector: "library" },
  { id: "claim-ledger", storageVariant: "claims", title: "Claim Ledger", sub: "Claims", icon: ScrollText, live: false, inspector: "info",
    description: "The research claim ledger. Populated once the PI starts framing claims." },
  { id: "experiment-ledger", storageVariant: "experiments", title: "Experiment Ledger", sub: "Runs", icon: FlaskConical, live: false, inspector: "info",
    description: "Benchmark/experiment runs. Populated once the experiments agent is live." },
];
const STORAGE: NodeDef[] = STORAGE_SRC.map((s, i) => ({
  ...s, type: "storage" as const, x: 56 + i * 192, y: 600, w: 178, h: 128,
}));

// --- Evaluation & output (dormant) --------------------------------------
const EVALUATION: NodeDef[] = [
  { id: "critic", x: 1088, y: 372, w: 264, h: 120, title: "Critic", sub: "Challenges & tests", icon: Microscope,
    description: "Challenges claims and probes their weaknesses before they advance." },
  { id: "gate-promotion", x: 1372, y: 372, w: 252, h: 120, title: "Gate", sub: "Promotion & approval", icon: ShieldCheck,
    description: "Runs the promotion gate — approve, hold, reject, or merge a claim." },
  { id: "experiments", x: 1088, y: 512, w: 264, h: 120, title: "Experiments Lab", sub: "Benchmarks & evals", icon: FlaskConical,
    description: "Runs benchmarks and evaluations against the corpus." },
  { id: "publication", x: 1372, y: 512, w: 252, h: 120, title: "Publication", sub: "Write & assemble", icon: PenLine,
    description: "Assembles and publishes the lab's findings." },
].map((n) => ({ ...n, type: "dormant" as const, live: false, inspector: "info" as const }));

// --- Operations (live) ---------------------------------------------------
const OPERATIONS: NodeDef[] = [
  { id: "ops", type: "ops", x: 56, y: 812, w: 1572, h: 170, title: "Ops / Quartermaster", sub: "Infrastructure & resources",
    icon: Cpu, live: true },
];

export const NODE_DEFS: NodeDef[] = [...WINGS, ...SCOUTS, ...RESEARCH, ...CORE, ...STORAGE, ...EVALUATION, ...OPERATIONS];

export const EDGE_DEFS: EdgeDef[] = [
  // scouts -> Mimir (intake), heat scoped to each scout's own discoveries
  ...SCOUTS.map((s) => ({
    id: `intake-${s.id}`, source: s.id, target: "mimir", sourceHandle: "b" as Handle, targetHandle: "t" as Handle,
    kind: "intake" as const, live: true, hotEvents: SCOUT_EVENTS, sourceKind: s.sourceKind,
  })),
  // Mimir -> Library (certified knowledge flows into the Library box)
  { id: "k-lib", source: "mimir", target: "library-box", sourceHandle: "b", targetHandle: "t", kind: "knowledge", live: true, hotEvents: LIB_EVENTS },
  // dormant handoffs (planned, never heat)
  { id: "w-queue", source: "mimir", target: "request-queue", sourceHandle: "r", targetHandle: "l", kind: "workflow", live: false, hotEvents: [] },
  { id: "w-eval", source: "library-box", target: "experiments", sourceHandle: "r", targetHandle: "l", kind: "workflow", live: false, hotEvents: [] },
];
