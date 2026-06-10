// Topology for the React Flow floorplan: wing backdrops, agent/storage/ops
// nodes, and the flow edges between them. Positions are hand-authored to match
// the mockup's wings (not auto-laid-out). Live data is injected at render time;
// this file is the static structure only.

import {
  Archive, Boxes, Calendar, Compass, Cpu, Database, FileText, FlaskConical, Github, Globe,
  Inbox, Microscope, Network, PenLine, ScrollText, ShieldCheck, Users, type LucideIcon,
} from "lucide-react";

export type FloorNodeType = "wing" | "scout" | "mimir" | "storage" | "ops" | "dormant" | "librarybox";
export type InspectorKind = "mimir" | "library" | "scout" | "gate" | "ariadne" | "queue" | "planner" | "researcher" | "ops" | "info";
export type Handle = "t" | "b" | "l" | "r" | "ts";  // ts = top-source (return/feedback edges)

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
  kind: "intake" | "knowledge" | "workflow" | "converse" | "feedback";
  live: boolean;
  hotEvents: string[];
  sourceKind?: string; // scope heat to one scout kind
}

const SCOUT_EVENTS = ["source.discovered", "library.sweep_requested", "library.trends"];
const LIB_EVENTS = ["document.ingested", "document.parsed"];
const ACQUIRE_EVENTS = ["acquire.requested", "acquire.fulfilled", "acquire.rejected"];
const EXP_EVENTS = ["experiment.requested", "experiment.completed", "experiment.failed"];

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
    inspector: "ariadne" as const,
    description: "Frames research directions and decides what to pursue or kill." },
  { id: "planner", x: 1284, y: 112, w: 150, h: 156, title: "Planner", sub: "Schedules & goals", icon: Calendar,
    inspector: "planner" as const,
    description: "Turns Ariadne's approved directions into concrete, falsifiable research tasks." },
  { id: "researchers", x: 1450, y: 112, w: 186, h: 156, title: "Researchers", sub: "Investigate & gather", icon: Users,
    inspector: "researcher" as const,
    description: "A pool of up to 4 researchers runs in parallel, each executing one task against the Library — grounded findings that steer each direction. When a direction needs a test, a researcher requests an experiment." },
].map((n) => ({ ...n, type: "dormant" as const, live: false }));

// --- Knowledge core: Mimir + request queue + storage row ----------------
const CORE: NodeDef[] = [
  { id: "mimir", type: "mimir", x: 72, y: 356, w: 372, h: 196, title: "Mimir", sub: "AI Curator of Knowledge",
    icon: ShieldCheck, live: true, inspector: "mimir" },
  { id: "request-queue", type: "dormant", x: 720, y: 356, w: 280, h: 196, title: "Request Queue", sub: "Acquire asks", icon: Inbox,
    live: false, inspector: "queue",
    description: "Agents ask Mimir to acquire specific evidence; Mimir resolves, dedupes, and trust-gates the ingest." },
  // Decorative containers that group the storage cards. LIBRARY = the queryable corpus
  // (raw / vector / graph); LEDGERS = the research record (claims / runs). Both open the
  // cards' own inspectors. Library anchors the Mimir -> Library flow.
  { id: "library-box", type: "librarybox", x: 44, y: 572, w: 582, h: 172, title: "Library", sub: "Queryable research memory", live: true },
  { id: "ledgers-box", type: "librarybox", x: 660, y: 572, w: 394, h: 172, title: "Ledgers", sub: "Research record", live: true },
];

const STORAGE_SRC: Array<Pick<NodeDef, "id" | "storageVariant" | "title" | "sub" | "icon" | "live" | "inspector" | "description">> = [
  { id: "raw-archive", storageVariant: "raw", title: "Raw Archive", sub: "Documents & files", icon: Archive, live: true, inspector: "library" },
  { id: "vector-memory", storageVariant: "vector", title: "Vector Memory", sub: "Embeddings & chunks", icon: Boxes, live: true, inspector: "library" },
  { id: "context-graph", storageVariant: "graph", title: "Context Graph", sub: "Nodes & relationships", icon: Network, live: true, inspector: "library" },
  { id: "claim-ledger", storageVariant: "claims", title: "Claim Ledger", sub: "Claims", icon: ScrollText, live: false, inspector: "ariadne",
    description: "The research claim ledger — Ariadne's mission + directions." },
  { id: "experiment-ledger", storageVariant: "experiments", title: "Run Ledger", sub: "Experiments", icon: FlaskConical, live: true, inspector: "ops",
    description: "Every sandboxed experiment run — code, budgets, resource usage, result, and the researcher's note — provenance for reproducibility." },
];
// First 3 cards sit in the LIBRARY box; the 2 ledgers (i >= 3) shift right into the LEDGERS box.
const STORAGE: NodeDef[] = STORAGE_SRC.map((s, i) => ({
  ...s, type: "storage" as const, x: 56 + i * 192 + (i >= 3 ? 40 : 0), y: 600, w: 178, h: 128,
}));

// --- Evaluation & output (mixed: experiments live, rest dormant) ---------
const EVALUATION: NodeDef[] = [
  { id: "critic", x: 1088, y: 372, w: 264, h: 120, title: "Critic", sub: "Challenges & tests", icon: Microscope,
    description: "Challenges claims and probes their weaknesses before they advance." },
  { id: "gate-promotion", x: 1372, y: 372, w: 252, h: 120, title: "Gate", sub: "Promotion & approval", icon: ShieldCheck,
    description: "Runs the promotion gate — approve, hold, reject, or merge a claim." },
  // LIVE: the sandboxed-experiment lane. A researcher's needs_experiment designs a
  // self-contained script (DeepSeek), the Quartermaster runs it in an isolated Docker
  // container, then it's interpreted into confidence feedback + a first-party Library note.
  { id: "experiments", x: 1088, y: 512, w: 264, h: 120, title: "Experiments", sub: "Sandboxed code runs", icon: FlaskConical,
    live: true, inspector: "ops" as const,
    description: "Designs and runs sandboxed ML experiments in isolated Docker containers (CPU + GPU). A researcher requests one, the agent writes the code, the Quartermaster allocates compute and runs it, and the result becomes confidence feedback + a Library note." },
  { id: "publication", x: 1372, y: 512, w: 252, h: 120, title: "Publication", sub: "Write & assemble", icon: PenLine,
    description: "Assembles and publishes the lab's findings." },
].map((n) => ({
  ...n,
  type: "dormant" as const,
  live: (n as NodeDef).live ?? false,
  inspector: (n as NodeDef).inspector ?? ("info" as const),
}));

// --- Operations (live) ---------------------------------------------------
const OPERATIONS: NodeDef[] = [
  { id: "ops", type: "ops", x: 56, y: 812, w: 1572, h: 170, title: "Ops / Quartermaster", sub: "Compute & experiment allocation",
    icon: Cpu, live: true, inspector: "ops" },
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
  { id: "w-queue", source: "mimir", target: "request-queue", sourceHandle: "r", targetHandle: "l", kind: "workflow", live: true, hotEvents: ACQUIRE_EVENTS },
  // Ariadne's wiring — kept short so no path crosses a block. She ASKS the Request Queue for
  // evidence (live; pulses on acquire) — her interface to the knowledge core (queue → Mimir →
  // Library) — and feeds the Planner → Researchers pipeline (planned until they wake).
  { id: "w-aria-queue", source: "ariadne", target: "request-queue", sourceHandle: "b", targetHandle: "t", kind: "knowledge", live: true, hotEvents: ACQUIRE_EVENTS },
  // Ariadne CONVERSES with Mimir directly (GraphRAG multi-hop ask) — its own violet "converse"
  // kind, threaded above the queue into Mimir's top so the path crosses no block. Pulses while a
  // question/answer is in flight.
  { id: "w-aria-mimir", source: "ariadne", target: "mimir", sourceHandle: "b", targetHandle: "t", kind: "converse", live: true, hotEvents: ["mimir.ask", "mimir.answered"] },
  { id: "w-aria-plan", source: "ariadne", target: "planner", sourceHandle: "r", targetHandle: "l", kind: "workflow", live: true, hotEvents: ["planner.plan", "task.created"] },
  // Planner hands tasks to the Researchers (live; pulses as tasks are created + completed).
  { id: "w-plan-research", source: "planner", target: "researchers", sourceHandle: "r", targetHandle: "l", kind: "workflow", live: true, hotEvents: ["task.created", "task.completed"] },
  // The Researchers ASK the Request Queue for evidence too (self-healing acquires) — threaded
  // bottom→top below the research wing so it crosses no block, like Ariadne's queue edge.
  { id: "w-research-queue", source: "researchers", target: "request-queue", sourceHandle: "b", targetHandle: "t", kind: "knowledge", live: true, hotEvents: ACQUIRE_EVENTS },
  // The FEEDBACK loop that CLOSES the autonomous cycle: research findings move each direction's
  // confidence/last_evidence, which Ariadne's reflection reads to steer. Routed top→top so it
  // arcs OVER the Planner (both top handles are otherwise free) — crosses no block. Amber.
  { id: "w-research-aria", source: "researchers", target: "ariadne", sourceHandle: "ts", targetHandle: "t", kind: "feedback", live: true, hotEvents: ["claim.confidence_changed", "task.completed"] },
  // --- Experiment lane (live) ---------------------------------------------------------------
  // A researcher that hits needs_experiment asks the Experiments agent for a test (it designs a
  // self-contained script). Pulses on experiment.requested.
  { id: "w-research-exp", source: "researchers", target: "experiments", sourceHandle: "b", targetHandle: "t", kind: "workflow", live: true, hotEvents: EXP_EVENTS },
  // The Experiments agent hands the queued run to the Quartermaster, which allocates compute and
  // runs it in an isolated Docker container (CPU/GPU). Pulses across the run's lifecycle.
  { id: "w-exp-ops", source: "experiments", target: "ops", sourceHandle: "b", targetHandle: "t", kind: "workflow", live: true, hotEvents: EXP_EVENTS },
  // The loop closes: the result + the researcher's note become a first-party Library document
  // (via Mimir's trust gate), so the lab's own experiments compound into the corpus. Pulses on
  // the result's source.discovered.
  { id: "w-exp-lib", source: "experiments", target: "library-box", sourceHandle: "l", targetHandle: "r", kind: "knowledge", live: true, hotEvents: ["source.discovered", "experiment.completed"] },
];
