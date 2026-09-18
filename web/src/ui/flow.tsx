// SVG flow-canvas primitives for the Pipeline view.
// Hand-rolled (no diagram libs): nodes are <g class="pipe-node"> groups laid
// out on a fixed viewBox; edges are bezier paths with a dashed "flow"
// animation class the page toggles when traffic is moving.

import type { ReactNode } from "react";
import type { DestinationNode } from "../api";
import { IconCheck, IconX } from "./icons";

export interface FlowNode {
  key?: string;
  title: string;
  lines: string[];
  x: number;
  y: number;
  w?: number;
  h?: number;
  /** status dot color token suffix: green | gray | yellow | red | accent */
  dot: string;
  selected?: boolean;
  onClick?: () => void;
  badge?: ReactNode;
}

const NODE_W = 148;
const NODE_H = 64;

export function PipeNode({ title, lines, x, y, w = NODE_W, dot, selected, onClick }: FlowNode) {
  const cx = x + w / 2;
  return (
    <g
      className={`pipe-node${selected ? " selected" : ""}`}
      transform={`translate(${x}, ${y})`}
      onClick={onClick}
      // role="button" is only meaningful when the node acts; without a handler
      // it must stay a plain group or AT announces a dead control (2.1.1).
      role={onClick ? "button" : undefined}
      aria-label={title}
      // A SVG group is not focusable by default and gets no key events — both
      // have to be wired explicitly for keyboard parity with the click (2.1.1).
      tabIndex={onClick ? 0 : undefined}
      onKeyDown={
        onClick
          ? (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onClick();
              }
            }
          : undefined
      }
    >
      <rect width={w} height={NODE_H} rx={10} />
      <circle className={`pipe-dot ${dot}`} cx={20} cy={NODE_H / 2} r={5} />
      <text className="pipe-node-title" x={34} y={24}>{title}</text>
      {lines.map((line, i) => (
        <text key={i} className="pipe-node-count" x={34} y={42 + i * 15}>{line}</text>
      ))}
      <title>{`${title}: ${lines.join(" · ") || "no activity"}`}</title>
      {/* invisible extended hit area so small nodes are easy to click */}
      <rect x={-4} y={-4} width={w + 8} height={NODE_H + 8} fill="transparent" />
      <circle cx={cx} cy={0} r={0} />
    </g>
  );
}

/** Bezier edge between two node anchor points; `flow` animates dashes. */
export function PipeEdge({
  from, to, flow, warn,
}: {
  from: [number, number];
  to: [number, number];
  flow?: boolean;
  warn?: boolean;
}) {
  const [x1, y1] = from;
  const [x2, y2] = to;
  const mx = (x1 + x2) / 2;
  const d = `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`;
  return (
    <path
      className={`edge${flow ? " flow" : ""}${warn ? " warn" : ""}`}
      d={d}
      markerEnd="url(#arrow)"
    />
  );
}

export function Defs() {
  return (
    <defs>
      <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5"
        markerWidth="7" markerHeight="7" orient="auto-start-reverse">
        <path d="M 0 1 L 9 5 L 0 9" fill="none" stroke="var(--border-strong)" strokeWidth="1.5" />
      </marker>
    </defs>
  );
}

/** Worst-state dot color for a destination node. */
export function destinationDot(d: DestinationNode): string {
  const { complete, sending, waiting, error } = d.routes;
  if (error > 0) return "red";
  if (d.health && d.health.status !== "ok") return "red";
  if (sending > 0 || waiting > 0) return "yellow";
  if (complete > 0) return "green";
  return "gray";
}

export function destinationLines(d: DestinationNode): string[] {
  const { complete, sending, waiting, error } = d.routes;
  const parts: string[] = [];
  if (complete > 0) parts.push(`✔ ${complete} sent`);
  if (sending > 0) parts.push(`↟ ${sending} sending`);
  if (waiting > 0) parts.push(`${waiting} waiting`);
  if (error > 0) parts.push(`✗ ${error} error`);
  if (parts.length === 0) parts.push("idle");
  if (d.health) {
    parts.push(`${d.health.status} · ${d.health.latency_ms}ms`);
  }
  return parts;
}

export function HealthBadge({ health }: { health?: DestinationNode["health"] | null }) {
  if (!health) return null;
  const cls = health.status === "ok" ? "green" : "red";
  return (
    <span className={`badge ${cls}`} title={`checked ${health.checked_at}`}>
      {health.status === "ok" ? <IconCheck size={11} /> : <IconX size={11} />} {health.status}
    </span>
  );
}
