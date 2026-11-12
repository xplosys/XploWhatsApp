import { Spinner } from "./Spinner";

type Variant = "primary" | "secondary" | "danger" | "ghost";

// Pill buttons with the Xplosys gradient, as on xplosys.com.
const styles: Record<Variant, string> = {
  primary:
    "bg-brand-gradient text-white shadow-brand bg-[length:160%_160%] bg-left-top hover:bg-right-bottom hover:-translate-y-0.5 transition-[transform,background-position,box-shadow] duration-300",
  secondary: "bg-surface text-ink shadow-[inset_0_0_0_1.5px_var(--xp-line)] hover:shadow-[inset_0_0_0_1.5px_var(--xp-ring)]",
  danger: "bg-surface text-danger shadow-[inset_0_0_0_1.5px_var(--xp-line)] hover:bg-danger-soft",
  ghost: "text-muted hover:text-ink hover:bg-surface-muted",
};

export function Button({
  variant = "primary",
  loading = false,
  className = "",
  children,
  disabled,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; loading?: boolean }) {
  return (
    <button
      {...props}
      disabled={disabled || loading}
      className={`inline-flex items-center justify-center gap-2 rounded-full px-5 py-2.5 text-sm font-bold transition-colors disabled:pointer-events-none disabled:opacity-60 ${styles[variant]} ${className}`}
    >
      {loading && <Spinner className="h-4 w-4" />}
      {children}
    </button>
  );
}
