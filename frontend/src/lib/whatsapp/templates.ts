import type { MessageTemplate } from "@/lib/api/types";

/** Number of distinct {{n}} placeholders in a template's body. */
export function placeholderCount(template: MessageTemplate | undefined): number {
  const body = template?.components.find((c) => c.type === "BODY")?.text ?? "";
  return new Set(body.match(/\{\{\d+\}\}/g) ?? []).size;
}

/** The send-API fields for a template with its body values filled in. */
export function templateFields(template: MessageTemplate, values: string[]): Record<string, unknown> {
  const count = placeholderCount(template);
  const fields: Record<string, unknown> = { template_name: template.name, template_language: template.language };
  if (count > 0) {
    fields.template_components = [
      { type: "body", parameters: Array.from({ length: count }, (_, i) => ({ type: "text", text: values[i] ?? "" })) },
    ];
  }
  return fields;
}
