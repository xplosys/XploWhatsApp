"use client";

/*
 * Small, dependency-free charts for the analytics screens. They follow one set of rules:
 * thin marks, a recessive grid, a legend for two or more series, text in text colours
 * (never the series colour), a hover/focus readout on every mark, and a table view that
 * shows every value without hovering. Series colours come from --color-series-* in
 * globals.css, validated for colour-vision deficiency in light and dark mode.
 */

import { useEffect, useId, useRef, useState } from "react";

export interface Series<K extends string> {
  key: K;
  label: string;
  /** A CSS colour, normally var(--color-series-N). */
  color: string;
}

export function compact(n: number): string {
  if (Math.abs(n) < 10_000) return n.toLocaleString();
  return new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(n);
}

/** "2026-11-13" as a short local date, without shifting it through UTC. */
export function dayLabel(day: string, opts: Intl.DateTimeFormatOptions = { day: "numeric", month: "short" }): string {
  const [y, m, d] = day.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString(undefined, opts);
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 60) return `${Math.max(1, Math.round(seconds))} s`;
  const minutes = seconds / 60;
  if (minutes < 60) return `${Math.round(minutes)} min`;
  const hours = minutes / 60;
  if (hours < 48) return `${hours < 10 ? hours.toFixed(1) : Math.round(hours)} h`;
  return `${Math.round(hours / 24)} days`;
}

/** A clean axis: four steps of 1, 2 or 5 x 10^n, so every tick is a whole, round number. */
function niceScale(v: number): { max: number; step: number } {
  const raw = Math.max(1, v) / 4;
  const pow = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((m) => m * pow).find((s) => s >= raw && s >= 1) ?? 10 * pow;
  return { max: Math.ceil(Math.max(1, v) / step) * step, step };
}

function useWidth<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => setWidth(Math.floor(entry.contentRect.width)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width];
}

// ---- legend and keys --------------------------------------------------------------

export function Legend<K extends string>({ series, mark = "line" }: { series: Series<K>[]; mark?: "line" | "rect" }) {
  return (
    <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-2">
      {series.map((s) => (
        <li key={s.key} className="flex items-center gap-1.5">
          <span
            aria-hidden
            className={mark === "line" ? "h-0.5 w-3.5 rounded-full" : "h-2.5 w-2.5 rounded-[3px]"}
            style={{ background: s.color }}
          />
          {s.label}
        </li>
      ))}
    </ul>
  );
}

/** A chart with a "Table" switch, so every value is readable without hovering. */
export function ChartFrame({
  title,
  description,
  legend,
  table,
  dimmed,
  children,
}: {
  title: string;
  description?: string;
  legend?: React.ReactNode;
  table: React.ReactNode;
  dimmed?: boolean;
  children: React.ReactNode;
}) {
  const [showTable, setShowTable] = useState(false);
  return (
    <section className="min-w-0 rounded-card border border-line bg-surface p-5 shadow-card sm:p-6">
      <header className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-base font-semibold">{title}</h2>
          {description && <p className="mt-1 text-sm text-muted">{description}</p>}
        </div>
        <button
          type="button"
          aria-pressed={showTable}
          onClick={() => setShowTable((v) => !v)}
          className="rounded-full border border-line px-3 py-1 text-xs font-medium text-ink-2 hover:border-ring hover:text-brand"
        >
          {showTable ? "Show chart" : "Show table"}
        </button>
      </header>
      {!showTable && legend && <div className="mb-3">{legend}</div>}
      <div className={`transition-opacity ${dimmed ? "opacity-50" : ""}`}>{showTable ? table : children}</div>
    </section>
  );
}

export function DataTable({ head, rows }: { head: string[]; rows: (string | number)[][] }) {
  return (
    <div className="max-h-80 overflow-auto">
      <table className="w-full text-sm">
        <thead className="sticky top-0 bg-surface text-left text-xs text-muted">
          <tr className="border-b border-line">
            {head.map((h, i) => (
              <th key={h} className={`py-2 pr-3 font-medium ${i ? "text-right" : ""}`}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="tabular-nums">
          {rows.map((r, i) => (
            <tr key={i} className="border-b border-line last:border-0">
              {r.map((v, j) => (
                <td key={j} className={`py-1.5 pr-3 ${j ? "text-right" : ""}`}>
                  {typeof v === "number" ? v.toLocaleString() : v}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---- tooltip ------------------------------------------------------------------------

interface TipRow {
  label: string;
  value: string;
  color?: string;
}

function Tooltip({ x, y, width, title, rows }: { x: number; y: number; width: number; title: string; rows: TipRow[] }) {
  const left = Math.min(Math.max(x + 12, 0), Math.max(0, width - 180));
  return (
    <div
      role="status"
      className="pointer-events-none absolute z-10 w-[168px] rounded-control border border-line bg-surface px-3 py-2 text-xs shadow-card"
      style={{ left, top: Math.max(0, y) }}
    >
      <p className="mb-1 text-muted">{title}</p>
      <ul className="space-y-0.5">
        {rows.map((r) => (
          <li key={r.label} className="flex items-center gap-2">
            {r.color && <span aria-hidden className="h-0.5 w-3 shrink-0 rounded-full" style={{ background: r.color }} />}
            <span className="font-semibold tabular-nums text-ink">{r.value}</span>
            <span className="truncate text-muted">{r.label}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

// ---- line chart ---------------------------------------------------------------------

const PAD = { top: 8, right: 12, bottom: 24, left: 36 };

export function LineChart<K extends string>({
  points,
  series,
  xLabel,
  height = 220,
}: {
  points: (Record<K, number> & { x: string })[];
  series: Series<K>[];
  xLabel: (x: string) => string;
  height?: number;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const id = useId();
  const { max, step: tickStep } = niceScale(Math.max(...points.flatMap((p) => series.map((s) => p[s.key])), 1));
  const plotW = Math.max(0, width - PAD.left - PAD.right);
  const plotH = height - PAD.top - PAD.bottom;
  const step = points.length > 1 ? plotW / (points.length - 1) : 0;
  const xAt = (i: number) => PAD.left + (points.length > 1 ? i * step : plotW / 2);
  const yAt = (v: number) => PAD.top + plotH - (v / max) * plotH;
  const ticks = Array.from({ length: Math.round(max / tickStep) + 1 }, (_, i) => i * tickStep);
  const labelEvery = Math.max(1, Math.ceil(points.length / Math.max(2, Math.floor(plotW / 64))));

  function pick(clientX: number) {
    const el = ref.current;
    if (!el || points.length === 0) return;
    const x = clientX - el.getBoundingClientRect().left - PAD.left;
    setHover(Math.min(points.length - 1, Math.max(0, Math.round(step ? x / step : 0))));
  }

  return (
    <div ref={ref} className="relative w-full select-none" style={{ height }}>
      {width > 0 && (
        <svg
          width={width}
          height={height}
          role="img"
          aria-labelledby={`${id}-desc`}
          tabIndex={0}
          className="block outline-none focus-visible:ring-2 focus-visible:ring-ring"
          onPointerMove={(e) => pick(e.clientX)}
          onPointerLeave={() => setHover(null)}
          onFocus={() => setHover(points.length - 1)}
          onBlur={() => setHover(null)}
          onKeyDown={(e) => {
            if (e.key === "ArrowLeft") setHover((h) => Math.max(0, (h ?? 0) - 1));
            if (e.key === "ArrowRight") setHover((h) => Math.min(points.length - 1, (h ?? -1) + 1));
          }}
        >
          <desc id={`${id}-desc`}>
            {series.map((s) => `${s.label}: ${points.reduce((a, p) => a + p[s.key], 0).toLocaleString()} in total`).join(". ")}
          </desc>
          {ticks.map((t) => (
            <g key={t}>
              <line x1={PAD.left} x2={width - PAD.right} y1={yAt(t)} y2={yAt(t)} stroke="var(--color-grid)" strokeWidth={1} />
              <text x={PAD.left - 8} y={yAt(t)} dy="0.32em" textAnchor="end" className="fill-muted text-[10px] tabular-nums">
                {compact(t)}
              </text>
            </g>
          ))}
          {points.map((p, i) =>
            i % labelEvery === 0 || i === points.length - 1 ? (
              i === points.length - 1 || points.length - 1 - i >= labelEvery / 2 ? (
                <text
                  key={p.x}
                  x={xAt(i)}
                  y={height - 6}
                  textAnchor={i === 0 ? "start" : i === points.length - 1 ? "end" : "middle"}
                  className="fill-muted text-[10px]"
                >
                  {xLabel(p.x)}
                </text>
              ) : null
            ) : null,
          )}
          {series.map((s) => (
            <g key={s.key}>
              <path
                d={points.map((p, i) => `${i ? "L" : "M"}${xAt(i)},${yAt(p[s.key])}`).join("")}
                fill="none"
                stroke={s.color}
                strokeWidth={2}
                strokeLinejoin="round"
                strokeLinecap="round"
              />
              {points.length === 1 && <circle cx={xAt(0)} cy={yAt(points[0][s.key])} r={4} fill={s.color} />}
            </g>
          ))}
          {hover !== null && points[hover] && (
            <g>
              <line x1={xAt(hover)} x2={xAt(hover)} y1={PAD.top} y2={PAD.top + plotH} stroke="var(--color-muted)" strokeWidth={1} />
              {series.map((s) => (
                <circle
                  key={s.key}
                  cx={xAt(hover)}
                  cy={yAt(points[hover][s.key])}
                  r={4}
                  fill={s.color}
                  stroke="var(--color-surface)"
                  strokeWidth={2}
                />
              ))}
            </g>
          )}
        </svg>
      )}
      {hover !== null && points[hover] && (
        <Tooltip
          x={xAt(hover)}
          y={PAD.top}
          width={width}
          title={xLabel(points[hover].x)}
          rows={series.map((s) => ({ label: s.label, value: points[hover][s.key].toLocaleString(), color: s.color }))}
        />
      )}
    </div>
  );
}

// ---- horizontal bars (one series) ---------------------------------------------------

export function BarList({ items, color = "var(--color-series-2)" }: { items: { label: string; value: number }[]; color?: string }) {
  const max = Math.max(1, ...items.map((i) => i.value));
  return (
    <ul className="space-y-3">
      {items.map((i) => (
        <li key={i.label} className="text-sm">
          <div className="mb-1 flex items-baseline justify-between gap-3">
            <span className="min-w-0 truncate text-ink-2">{i.label}</span>
            <span className="font-semibold tabular-nums">{i.value.toLocaleString()}</span>
          </div>
          <div className="h-2 rounded-full" aria-hidden>
            <div
              className="h-2 rounded-r-[4px]"
              style={{ width: `${Math.max(2, (i.value / max) * 100)}%`, background: color, maxWidth: "100%" }}
            />
          </div>
        </li>
      ))}
    </ul>
  );
}

// ---- weekday x hour heatmap -----------------------------------------------------------

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const STEPS = [18, 36, 56, 78, 100];

export function hourLabel(h: number): string {
  return new Date(2000, 0, 1, h).toLocaleTimeString(undefined, { hour: "numeric" });
}

export function Heatmap({ cells }: { cells: { weekday: number; hour: number; messages: number }[] }) {
  const [hover, setHover] = useState<{ weekday: number; hour: number; x: number; y: number } | null>(null);
  const [ref, width] = useWidth<HTMLDivElement>();
  const grid = new Map(cells.map((c) => [`${c.weekday}-${c.hour}`, c.messages]));
  const max = Math.max(1, ...cells.map((c) => c.messages));
  const fill = (v: number) => {
    if (!v) return "var(--color-surface-muted)";
    const step = STEPS[Math.min(STEPS.length - 1, Math.floor((v / max) * STEPS.length - 1e-9))];
    return `color-mix(in oklab, var(--color-series-2) ${step}%, var(--color-surface))`;
  };
  const value = hover ? grid.get(`${hover.weekday}-${hover.hour}`) ?? 0 : 0;

  return (
    <div ref={ref} className="relative">
      <div className="overflow-x-auto">
        <div className="grid min-w-[520px] grid-cols-[36px_repeat(24,minmax(0,1fr))] gap-[2px]" onPointerLeave={() => setHover(null)}>
          <span />
          {Array.from({ length: 24 }, (_, h) => (
            <span key={h} className="text-center text-[10px] text-muted">
              {h % 3 === 0 ? hourLabel(h).replace(/\s/g, "") : ""}
            </span>
          ))}
          {WEEKDAYS.map((day, d) => (
            <div key={day} className="contents">
              <span className="pr-1 text-[11px] leading-5 text-muted">{day}</span>
              {Array.from({ length: 24 }, (_, h) => {
                const v = grid.get(`${d + 1}-${h}`) ?? 0;
                const label = `${day} ${hourLabel(h)}: ${v.toLocaleString()} customer message${v === 1 ? "" : "s"}`;
                const show = (e: { currentTarget: HTMLElement }) => {
                  const box = e.currentTarget.getBoundingClientRect();
                  const root = ref.current?.getBoundingClientRect();
                  setHover({ weekday: d + 1, hour: h, x: box.left - (root?.left ?? 0), y: box.bottom - (root?.top ?? 0) + 4 });
                };
                return (
                  <span
                    key={h}
                    role="img"
                    aria-label={label}
                    onPointerEnter={show}
                    onFocus={show}
                    className={`h-5 rounded-[3px] ${hover?.weekday === d + 1 && hover.hour === h ? "ring-2 ring-ink" : ""}`}
                    style={{ background: fill(v) }}
                  />
                );
              })}
            </div>
          ))}
        </div>
      </div>
      <div className="mt-3 flex items-center gap-2 text-[11px] text-muted" aria-hidden>
        <span>Fewer</span>
        <span className="h-3 w-4 rounded-[3px]" style={{ background: "var(--color-surface-muted)" }} />
        {STEPS.map((s) => (
          <span
            key={s}
            className="h-3 w-4 rounded-[3px]"
            style={{ background: `color-mix(in oklab, var(--color-series-2) ${s}%, var(--color-surface))` }}
          />
        ))}
        <span>More</span>
      </div>
      {hover && (
        <Tooltip
          x={hover.x}
          y={hover.y}
          width={width}
          title={`${WEEKDAYS[hover.weekday - 1]} · ${hourLabel(hover.hour)}`}
          rows={[{ label: "customer messages", value: value.toLocaleString() }]}
        />
      )}
    </div>
  );
}

export { WEEKDAYS };

// ---- stat tile ----------------------------------------------------------------------

export function StatTile({
  label,
  value,
  delta,
  hint,
}: {
  label: string;
  value: string;
  delta?: { text: string; tone: "good" | "bad" | "flat" } | null;
  hint?: string;
}) {
  const tones = { good: "text-success", bad: "text-danger", flat: "text-muted" };
  return (
    <div className="min-w-0 rounded-card border border-line bg-surface p-5 shadow-card">
      <p className="text-sm text-muted">{label}</p>
      <p className="mt-1 text-3xl font-semibold tracking-tight">{value}</p>
      {delta && (
        <p className={`mt-1 text-xs font-medium ${tones[delta.tone]}`}>
          {delta.text}
        </p>
      )}
      {hint && <p className="mt-1 text-xs text-muted">{hint}</p>}
    </div>
  );
}

/** Change against the previous period, coloured by whether up is good for this figure. */
export function change(
  now: number | null,
  before: number | null,
  { upIsGood = true, percentPoints = false }: { upIsGood?: boolean; percentPoints?: boolean } = {},
): { text: string; tone: "good" | "bad" | "flat" } | null {
  if (now === null || before === null) return null;
  if (percentPoints) {
    const diff = Math.round((now - before) * 100);
    if (diff === 0) return { text: "Same as the previous period", tone: "flat" };
    return { text: `${diff > 0 ? "▲" : "▼"} ${Math.abs(diff)} points vs previous period`, tone: diff > 0 === upIsGood ? "good" : "bad" };
  }
  if (before === 0) return now === 0 ? { text: "Same as the previous period", tone: "flat" } : { text: "New this period", tone: "flat" };
  const pct = Math.round(((now - before) / before) * 100);
  if (pct === 0) return { text: "Same as the previous period", tone: "flat" };
  return { text: `${pct > 0 ? "▲" : "▼"} ${Math.abs(pct)}% vs previous period`, tone: pct > 0 === upIsGood ? "good" : "bad" };
}
