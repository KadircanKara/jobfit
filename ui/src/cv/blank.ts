import type { Bullet, CvProfile, Entry, Variant } from "./api";

export const FIXED_KEYS = ["summary", "experience", "education", "skills", "languages", "projects"] as const;
export const FIXED_TITLES: Record<string, string> = {
  summary: "PROFESSIONAL SUMMARY",
  experience: "EXPERIENCE",
  education: "EDUCATION",
  skills: "SKILLS",
  languages: "LANGUAGES",
  projects: "PROJECTS",
};

/** Ids are made here, once, and never reused: they outlive every reorder. */
export function newId(): string {
  return crypto.randomUUID().replace(/-/g, "").slice(0, 12);
}

export function blankBullet(): Bullet {
  return { id: newId(), text: "", hidden: false, notes: "" };
}

export function blankEntry(): Entry {
  return {
    id: newId(),
    title: "",
    subtitle: "",
    location: "",
    start: "",
    end: "",
    url: "",
    gpa: "",
    hidden: false,
    notes: "",
    bullets: [],
  };
}

export function blankVariant(): Variant {
  return { id: newId(), label: "", text: "" };
}

export function blankProfile(): CvProfile {
  return {
    schema_version: 1,
    basics: { name: "", headline: "", headline_variants: [], email: "", phone: "", location: "", links: [] },
    extras: [],
    summary: { text: "", variants: [] },
    experience: [],
    education: [],
    projects: [],
    skills: [],
    languages: [],
    custom_sections: [],
    layout: FIXED_KEYS.map((key) => ({ key, title: FIXED_TITLES[key] })),
  };
}

// Lists are edited by position and replaced whole, so every change is a new array
// and React sees it.
export function replaceAt<T>(list: T[], index: number, next: T): T[] {
  return list.map((item, i) => (i === index ? next : item));
}

export function removeAt<T>(list: T[], index: number): T[] {
  return list.filter((_, i) => i !== index);
}

export function move<T>(list: T[], index: number, delta: number): T[] {
  const target = index + delta;
  if (target < 0 || target >= list.length) return list;
  const next = [...list];
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}
