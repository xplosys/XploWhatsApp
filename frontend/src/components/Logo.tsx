// Wordmark in the XploSys style: gradient capitals, medium weight, tight tracking.
export function Logo({
  className = "",
  tagline = false,
  onDark = false,
}: {
  className?: string;
  tagline?: boolean;
  onDark?: boolean;
}) {
  const grad = onDark ? "text-gradient-light" : "text-gradient";
  return (
    <span className={`inline-flex items-center gap-3 ${className}`}>
      <span className={`text-[1.35em] font-medium tracking-[-0.03em] ${onDark ? "text-white" : "text-ink"}`}>
        <span className={grad}>X</span>plo<span className={grad}>W</span>hatsApp
      </span>
      {tagline && (
        <span
          className={`relative hidden pl-3 text-[11px] leading-tight font-medium sm:block ${onDark ? "text-[#c7d2fe]" : "text-muted"}`}
        >
          <span
            aria-hidden
            className="absolute top-0.5 bottom-0.5 left-0 w-0.5 rounded bg-gradient-to-b from-[#4338ca] via-[#7c3aed] to-[#c026d3]"
          />
          by XploSys
        </span>
      )}
    </span>
  );
}
