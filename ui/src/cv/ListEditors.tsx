import type { Extra, Language, Link, SkillGroup, Variant } from "./api";
import { blankVariant, move, newId, removeAt, replaceAt } from "./blank";
import { Area, ChipInput, RowTools, Text, type Errors } from "./Controls";

export function LinksEditor({ links, errors, onChange }: { links: Link[]; errors: Errors; onChange: (next: Link[]) => void }) {
  return (
    <div className="cv-list">
      <div className="cv-list-head">Links</div>
      {links.map((link, i) => (
        // Links carry no id: they are a short list edited in place, keyed by position.
        <div className="cv-row" key={i}>
          <Text
            label="Label"
            value={link.label}
            path={`basics.links.${i}.label`}
            errors={errors}
            placeholder="LinkedIn"
            onChange={(label) => onChange(replaceAt(links, i, { ...link, label }))}
          />
          <Text
            label="URL"
            value={link.url}
            path={`basics.links.${i}.url`}
            errors={errors}
            placeholder="https://"
            wide
            onChange={(url) => onChange(replaceAt(links, i, { ...link, url }))}
          />
          <RowTools
            index={i}
            count={links.length}
            label="link"
            onMove={(delta) => onChange(move(links, i, delta))}
            onRemove={() => onChange(removeAt(links, i))}
          />
        </div>
      ))}
      <button type="button" className="btn ghost sm" onClick={() => onChange([...links, { label: "", url: "" }])}>
        Add link
      </button>
    </div>
  );
}

export function ExtrasEditor({ extras, errors, onChange }: { extras: Extra[]; errors: Errors; onChange: (next: Extra[]) => void }) {
  return (
    <div className="cv-list">
      <div className="cv-list-head">
        Labelled lines <span className="hint">printed under the header, e.g. MILITARY STATUS : Completed</span>
      </div>
      {extras.map((extra, i) => (
        <div className="cv-row" key={extra.id}>
          <Text
            label="Label"
            value={extra.label}
            path={`extras.${i}.label`}
            errors={errors}
            placeholder="MILITARY STATUS"
            onChange={(label) => onChange(replaceAt(extras, i, { ...extra, label }))}
          />
          <Text
            label="Value"
            value={extra.value}
            path={`extras.${i}.value`}
            errors={errors}
            placeholder="Completed"
            wide
            onChange={(value) => onChange(replaceAt(extras, i, { ...extra, value }))}
          />
          <RowTools
            index={i}
            count={extras.length}
            label="line"
            onMove={(delta) => onChange(move(extras, i, delta))}
            onRemove={() => onChange(removeAt(extras, i))}
          />
        </div>
      ))}
      <button
        type="button"
        className="btn ghost sm"
        onClick={() => onChange([...extras, { id: newId(), label: "", value: "" }])}
      >
        Add a labelled line
      </button>
    </div>
  );
}

export function VariantsEditor({
  title,
  hint,
  variants,
  path,
  errors,
  multiline,
  onChange,
}: {
  title: string;
  hint: string;
  variants: Variant[];
  path: string;
  errors: Errors;
  multiline: boolean;
  onChange: (next: Variant[]) => void;
}) {
  return (
    <div className="cv-list">
      <div className="cv-list-head">
        {title} <span className="hint">{hint}</span>
      </div>
      {variants.map((variant, i) => (
        <div className="cv-row" key={variant.id}>
          <Text
            label="Label"
            value={variant.label}
            path={`${path}.${i}.label`}
            errors={errors}
            placeholder="Energy / optimization roles"
            onChange={(label) => onChange(replaceAt(variants, i, { ...variant, label }))}
          />
          {multiline ? (
            <Area
              label="Text"
              value={variant.text}
              path={`${path}.${i}.text`}
              errors={errors}
              onChange={(text) => onChange(replaceAt(variants, i, { ...variant, text }))}
            />
          ) : (
            <Text
              label="Text"
              value={variant.text}
              path={`${path}.${i}.text`}
              errors={errors}
              wide
              onChange={(text) => onChange(replaceAt(variants, i, { ...variant, text }))}
            />
          )}
          <RowTools
            index={i}
            count={variants.length}
            label="variant"
            onMove={(delta) => onChange(move(variants, i, delta))}
            onRemove={() => onChange(removeAt(variants, i))}
          />
        </div>
      ))}
      <button type="button" className="btn ghost sm" onClick={() => onChange([...variants, blankVariant()])}>
        Add a variant
      </button>
    </div>
  );
}

export function SkillsEditor({ groups, errors, onChange }: { groups: SkillGroup[]; errors: Errors; onChange: (next: SkillGroup[]) => void }) {
  return (
    <div className="cv-list">
      {groups.map((group, i) => (
        <div className="cv-row" key={group.id}>
          <Text
            label="Category"
            value={group.category}
            path={`skills.${i}.category`}
            errors={errors}
            placeholder="Programming Languages"
            onChange={(category) => onChange(replaceAt(groups, i, { ...group, category }))}
          />
          <ChipInput
            items={group.items}
            placeholder="Type a skill, press Enter"
            onChange={(items) => onChange(replaceAt(groups, i, { ...group, items }))}
          />
          <RowTools
            index={i}
            count={groups.length}
            label={group.category || "category"}
            onMove={(delta) => onChange(move(groups, i, delta))}
            onRemove={() => onChange(removeAt(groups, i))}
          />
        </div>
      ))}
      <button
        type="button"
        className="btn ghost sm"
        onClick={() => onChange([...groups, { id: newId(), category: "", items: [] }])}
      >
        Add a category
      </button>
    </div>
  );
}

export function LanguagesEditor({
  languages,
  errors,
  onChange,
}: {
  languages: Language[];
  errors: Errors;
  onChange: (next: Language[]) => void;
}) {
  return (
    <div className="cv-list">
      {languages.map((language, i) => (
        <div className="cv-row" key={language.id}>
          <Text
            label="Language"
            value={language.name}
            path={`languages.${i}.name`}
            errors={errors}
            placeholder="English"
            onChange={(name) => onChange(replaceAt(languages, i, { ...language, name }))}
          />
          <Text
            label="Level"
            value={language.level}
            path={`languages.${i}.level`}
            errors={errors}
            placeholder="C1"
            onChange={(level) => onChange(replaceAt(languages, i, { ...language, level }))}
          />
          <Text
            label="Detail"
            value={language.detail}
            path={`languages.${i}.detail`}
            errors={errors}
            placeholder="IELTS: 7.5 Overall - Jan 2026"
            wide
            onChange={(detail) => onChange(replaceAt(languages, i, { ...language, detail }))}
          />
          <RowTools
            index={i}
            count={languages.length}
            label={language.name || "language"}
            onMove={(delta) => onChange(move(languages, i, delta))}
            onRemove={() => onChange(removeAt(languages, i))}
          />
        </div>
      ))}
      <button
        type="button"
        className="btn ghost sm"
        onClick={() => onChange([...languages, { id: newId(), name: "", level: "", detail: "" }])}
      >
        Add a language
      </button>
    </div>
  );
}
