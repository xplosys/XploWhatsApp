export function EmptyState({ title, description }: { title: string; description?: string }) {
  return (
    <div className="rounded-control border border-dashed border-line px-6 py-10 text-center">
      <p className="text-sm font-medium text-ink">{title}</p>
      {description && <p className="mt-1 text-sm text-muted">{description}</p>}
    </div>
  );
}
