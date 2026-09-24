import type { CustomSection, CvProfile, SectionRef } from "./api";
import { FIXED_KEYS, FIXED_TITLES, move, newId, removeAt, replaceAt } from "./blank";
import { Area, RowTools, Text, type Errors } from "./Controls";
import { EntryList } from "./EntryCard";
import { ExtrasEditor, LanguagesEditor, LinksEditor, SkillsEditor, VariantsEditor } from "./ListEditors";

type Props = { profile: CvProfile; errors: Errors; onChange: (next: CvProfile) => void };

const ENTRY_KEYS = ["experience", "education", "projects"] as const;
type EntryKey = (typeof ENTRY_KEYS)[number];
const ADD_LABEL: Record<EntryKey, string> = {
  experience: "Add a position",
  education: "Add a degree",
  projects: "Add a project",
};

function isEntryKey(key: string): key is EntryKey {
  return (ENTRY_KEYS as readonly string[]).includes(key);
}

/**
 * Every section of the CV as a card, in the order `layout` prints them. The
 * order here is the order on the page, so moving a card moves the section.
 */
export function ProfileForm({ profile, errors, onChange }: Props) {
  const set = <K extends keyof CvProfile>(key: K, value: CvProfile[K]) => onChange({ ...profile, [key]: value });
  const basics = profile.basics;
  const setBasics = (patch: Partial<CvProfile["basics"]>) => set("basics", { ...basics, ...patch });
  const shown = new Set(profile.layout.map((ref) => ref.key));
  const missing = FIXED_KEYS.filter((key) => !shown.has(key));

  function rename(index: number, ref: SectionRef, title: string) {
    const layout = replaceAt(profile.layout, index, { ...ref, title });
    // A custom section's own title follows its heading, so the two never disagree.
    const custom_sections = profile.custom_sections.map((custom) =>
      `custom:${custom.id}` === ref.key ? { ...custom, title } : custom,
    );
    onChange({ ...profile, layout, custom_sections });
  }

  function addCustom() {
    const custom: CustomSection = { id: newId(), title: "NEW SECTION", entries: [] };
    onChange({
      ...profile,
      custom_sections: [...profile.custom_sections, custom],
      layout: [...profile.layout, { key: `custom:${custom.id}`, title: custom.title }],
    });
  }

  function removeSection(index: number, ref: SectionRef) {
    const custom = ref.key.startsWith("custom:");
    const question = custom
      ? `Delete ${ref.title} and everything in it?`
      : `Stop printing ${ref.title}? What is in it is kept, and it can be shown again below.`;
    if (!window.confirm(question)) return;
    onChange({
      ...profile,
      layout: removeAt(profile.layout, index),
      custom_sections: custom
        ? profile.custom_sections.filter((section) => `custom:${section.id}` !== ref.key)
        : profile.custom_sections,
    });
  }

  return (
    <>
      <section className="panel">
        <div className="panel-head">
          <h2>About you</h2>
          <div className="note">the header of every CV</div>
        </div>
        <div className="fields">
          <Text label="Full name" value={basics.name} path="basics.name" errors={errors} onChange={(name) => setBasics({ name })} wide />
          <Text
            label="Headline"
            value={basics.headline}
            path="basics.headline"
            errors={errors}
            placeholder="M.Sc. Computer Science"
            onChange={(headline) => setBasics({ headline })}
            wide
          />
          <Text label="Email" value={basics.email} path="basics.email" errors={errors} onChange={(email) => setBasics({ email })} />
          <Text label="Phone" value={basics.phone} path="basics.phone" errors={errors} onChange={(phone) => setBasics({ phone })} />
          <Text
            label="Location"
            value={basics.location}
            path="basics.location"
            errors={errors}
            placeholder="Istanbul, Turkey"
            onChange={(location) => setBasics({ location })}
            wide
          />
        </div>
        <LinksEditor links={basics.links} errors={errors} onChange={(links) => setBasics({ links })} />
        <VariantsEditor
          title="Headline variants"
          hint="alternatives kept in master.tex as comments, for tailoring to pick from"
          variants={basics.headline_variants}
          path="basics.headline_variants"
          errors={errors}
          multiline={false}
          onChange={(headline_variants) => setBasics({ headline_variants })}
        />
        <ExtrasEditor extras={profile.extras} errors={errors} onChange={(extras) => set("extras", extras)} />
      </section>

      {profile.layout.map((ref, index) => (
        <section className="panel" key={ref.key}>
          <div className="panel-head cv-section-head">
            <input
              className="cv-section-title"
              aria-label="Section heading"
              value={ref.title}
              onChange={(event) => rename(index, ref, event.target.value)}
            />
            <RowTools
              index={index}
              count={profile.layout.length}
              label={ref.title}
              onMove={(delta) => set("layout", move(profile.layout, index, delta))}
              onRemove={() => removeSection(index, ref)}
            />
          </div>
          {errors[`layout.${index}.title`] && <div className="err">{errors[`layout.${index}.title`]}</div>}
          <SectionBody profile={profile} sectionKey={ref.key} errors={errors} onChange={onChange} />
        </section>
      ))}

      <div className="cv-addsection">
        {missing.map((key) => (
          <button
            type="button"
            className="btn ghost sm"
            key={key}
            onClick={() => set("layout", [...profile.layout, { key, title: FIXED_TITLES[key] }])}
          >
            Show {FIXED_TITLES[key].toLowerCase()}
          </button>
        ))}
        <button type="button" className="btn ghost sm" onClick={addCustom}>
          Add a custom section
        </button>
      </div>
    </>
  );
}

function SectionBody({ profile, sectionKey, errors, onChange }: Props & { sectionKey: string }) {
  const set = <K extends keyof CvProfile>(key: K, value: CvProfile[K]) => onChange({ ...profile, [key]: value });

  if (sectionKey === "summary") {
    return (
      <>
        <Area
          label="Summary"
          value={profile.summary.text}
          path="summary.text"
          errors={errors}
          rows={5}
          placeholder="Three to five sentences. **Bold** the terms a recruiter searches for."
          onChange={(text) => set("summary", { ...profile.summary, text })}
        />
        <VariantsEditor
          title="Summary variants"
          hint="alternatives kept in master.tex as comments"
          variants={profile.summary.variants}
          path="summary.variants"
          errors={errors}
          multiline
          onChange={(variants) => set("summary", { ...profile.summary, variants })}
        />
      </>
    );
  }
  if (sectionKey === "skills") {
    return <SkillsEditor groups={profile.skills} errors={errors} onChange={(skills) => set("skills", skills)} />;
  }
  if (sectionKey === "languages") {
    return (
      <LanguagesEditor languages={profile.languages} errors={errors} onChange={(languages) => set("languages", languages)} />
    );
  }
  if (isEntryKey(sectionKey)) {
    return (
      <EntryList
        entries={profile[sectionKey]}
        kind={sectionKey}
        path={sectionKey}
        errors={errors}
        addLabel={ADD_LABEL[sectionKey]}
        onChange={(entries) => set(sectionKey, entries)}
      />
    );
  }
  const index = profile.custom_sections.findIndex((custom) => `custom:${custom.id}` === sectionKey);
  if (index < 0) return null;
  const custom = profile.custom_sections[index];
  return (
    <EntryList
      entries={custom.entries}
      kind="custom"
      path={`custom_sections.${index}.entries`}
      errors={errors}
      addLabel="Add an entry"
      onChange={(entries) => set("custom_sections", replaceAt(profile.custom_sections, index, { ...custom, entries }))}
    />
  );
}
