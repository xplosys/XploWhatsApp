type Tone = "error" | "success" | "warning" | "info";

const tones: Record<Tone, string> = {
  error: "bg-danger-soft text-danger border-danger/20",
  success: "bg-success-soft text-success border-success/20",
  warning: "bg-warning-soft text-warning border-warning/20",
  info: "bg-brand-soft text-brand-ink border-ring",
};

export function Alert({ tone = "info", children }: { tone?: Tone; children: React.ReactNode }) {
  return (
    <div role={tone === "error" ? "alert" : "status"} className={`rounded-control border px-4 py-3 text-sm ${tones[tone]}`}>
      {children}
    </div>
  );
}
