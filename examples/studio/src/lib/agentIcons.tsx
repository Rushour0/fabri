// Monochrome line icons for the Company view — a cohesive set (Lucide, MIT) that
// replaces the emoji glyphs. Icons inherit `currentColor`, so they pick up the
// node's text/status color and the viewer's theme automatically. graph.ts assigns
// each agent a role key (see `iconFor`); this maps the key to an icon.
import {
  Briefcase, Search, Wrench, Microscope, PenLine, FlaskConical, ClipboardCheck,
  Map as MapIcon, Palette, Rocket, Shield, Database, Code2, Bot,
  FileText, Coins, CircleCheck, CircleX, type LucideIcon,
} from "lucide-react";

export type RoleIcon =
  | "manager" | "triage" | "fix" | "research" | "write" | "test" | "verify"
  | "plan" | "design" | "deploy" | "security" | "data" | "code" | "agent";

const ROLE_ICON: Record<RoleIcon, LucideIcon> = {
  manager: Briefcase,
  triage: Search,
  fix: Wrench,
  research: Microscope,
  write: PenLine,
  test: FlaskConical,
  verify: ClipboardCheck,
  plan: MapIcon,
  design: Palette,
  deploy: Rocket,
  security: Shield,
  data: Database,
  code: Code2,
  agent: Bot,
};

export function AgentIcon({ name, size = 24, className }: { name: string; size?: number; className?: string }) {
  const Icon = ROLE_ICON[name as RoleIcon] ?? Bot;
  return <Icon size={size} strokeWidth={1.75} className={className} aria-hidden />;
}

export function DocumentIcon({ size = 15 }: { size?: number }) {
  return <FileText size={size} strokeWidth={1.75} aria-hidden />;
}

export function CoinIcon({ size = 13, className }: { size?: number; className?: string }) {
  return <Coins size={size} strokeWidth={1.75} className={className} aria-hidden />;
}

export function StatusIcon({ status, size = 15 }: { status: string; size?: number }) {
  if (status === "done") return <CircleCheck size={size} strokeWidth={2} aria-hidden />;
  if (status === "error") return <CircleX size={size} strokeWidth={2} aria-hidden />;
  return null;
}
