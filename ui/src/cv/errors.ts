import type { Errors } from "./Controls";

/**
 * Server refusals arrive keyed by position ("experience.2.title"). After a move
 * or a removal that position holds a different item, so an error is kept only
 * while every list step on its path still reaches the item it was raised for.
 */
export function stillApplies(errors: Errors, then: unknown, now: unknown): Errors {
  const kept: Errors = {};
  for (const [path, message] of Object.entries(errors)) {
    if (sameTarget(path.split("."), then, now)) kept[path] = message;
  }
  return kept;
}

function sameTarget(parts: string[], then: unknown, now: unknown): boolean {
  let before = then;
  let after = now;
  for (const part of parts) {
    before = step(before, part);
    after = step(after, part);
    if (/^\d+$/.test(part) && idOf(before) !== idOf(after)) return false;
    if (after === undefined && before !== undefined) return false;
  }
  return true;
}

function step(value: unknown, key: string): unknown {
  return value !== null && typeof value === "object" ? (value as Record<string, unknown>)[key] : undefined;
}

function idOf(value: unknown): unknown {
  return step(value, "id");
}
