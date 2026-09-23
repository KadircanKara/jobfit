"""Whether a template is fit to hold the profile.

Four questions are asked of every uploaded or converted template before it can
be used. Each one catches a way a template can quietly let the person down:

- Does it load other files? A template is one .tex, and anything else it needs
  would be missing on the day it is used.
- Does it keep hidden items hidden? A probe profile carries a marker in a hidden
  bullet, a hidden entry and a note, and the marker may only ever sit in a comment.
- Does it print everything? Every visible word of the profile must appear in the
  filled template, so a section the template forgot is caught here rather than
  on an employer's desk.
- Does it compile? In the sandbox, like every build of an untrusted template.

Then the ATS check runs on the result. What it finds is a warning, not a
refusal: the look is the person's choice, and this says what it costs.
"""
from __future__ import annotations

import dataclasses
import pathlib
import re
import shutil
import subprocess

from jobhunt.config import Config
from jobhunt.cv import ats, builds, latex, markup, model, render, templates
from jobhunt.cv.words import split_comments, words

SENTINEL = "zqxhiddenprobe"

_INCLUDES = re.compile(
    r"\\(input|include|includegraphics|includepdf|includesvg|lstinputlisting|verbatiminput|import|subimport)"
    r"\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}"
)
# Commands that reach outside the page: Lua, and TeX's own file and shell access.
_REACHING = re.compile(
    r"\\(directlua|latelua|luaexec|luadirect|luafunction|openin|openout|write18|immediate\\write)\b"
    r"|\\begin\{(luacode)\*?\}"
)
_END = "\\end{document}"
_MAGIC = re.compile(r"^%\s*!TEX\s+(?:TS-)?program\s*=\s*(\w+)", re.MULTILINE | re.IGNORECASE)


@dataclasses.dataclass
class Findings:
    problems: list[str]
    warnings: list[str]
    tex: str = ""
    pdf: bytes = b""
    ats: ats.Report | None = None

    @property
    def ok(self) -> bool:
        return not self.problems


def validate(
    config: Config,
    source: str,
    *,
    profile: model.Profile,
    runner: latex.Runner | None = None,
    ats_runner: ats.Runner | None = None,
) -> Findings:
    problems = self_contained(source)
    if problems:
        return Findings(problems, [])
    try:
        tex = builds.fill(source, profile, trusted=False)
        probe = builds.fill(source, probe_profile(), trusted=False)
    except render.RenderError as exc:
        return Findings([f"it cannot be filled: {exc}"], [])
    if leaks(probe):
        problems.append(
            "it prints hidden items. wrap every entry, bullet and skills line in "
            "\\BLOCK{call hidable(item)} ... \\BLOCK{endcall}"
        )
    missing = missing_content(profile, tex)
    if missing:
        problems.append("it leaves parts of the profile out: " + ", ".join(missing[:30]))
    built = builds.build(config, tex, engine=engine_of(source), trusted=False, runner=runner)
    if not built.ok:
        problems.append("it does not compile:\n" + built.log[-2000:])
        return Findings(problems, [], tex)
    warnings = []
    code = split_comments(source)[0]
    reaching = sorted({name for match in _REACHING.findall(code) for name in match if name})
    if reaching:
        warnings.append(
            "it runs Lua or opens files (" + ", ".join(f"\\{name}" for name in reaching) + "). "
            "that only ever happens inside the sandbox, with no access to your files or the network"
        )
    if built.missing:
        warnings.append("its font cannot print " + " ".join(built.missing))
    report = ats.check(config, tex, built.pdf, runner=ats_runner)
    warnings += [f"ATS check: {failure}" for failure in report.failures]
    warnings += [f"ATS note: {note}" for note in report.warnings]
    return Findings(problems, warnings, tex, built.pdf, report)


def engine_of(source: str) -> str:
    """The engine a `% !TEX program = …` line names, if it is one we run."""
    match = _MAGIC.search(source)
    engine = match.group(1).lower() if match else latex.DEFAULT_ENGINE
    return engine if engine in latex.ENGINES else latex.DEFAULT_ENGINE


def self_contained(source: str) -> list[str]:
    problems = []
    for command, target in _INCLUDES.findall(split_comments(source)[0]):
        if command in ("input", "include") and _ships_with_tex(target.strip()):
            continue
        problems.append(
            f"it loads another file (\\{command}{{{target}}}). "
            "a template must be one self-contained .tex file"
        )
    return problems


def _ships_with_tex(name: str) -> bool:
    """`\\input{glyphtounicode}` is part of TeX; `\\input{../secrets}` is not."""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return False
    kpsewhich = shutil.which("kpsewhich")
    if kpsewhich is None:
        return False
    home = pathlib.Path.home().resolve()
    for candidate in (name, f"{name}.tex"):
        done = subprocess.run([kpsewhich, candidate], capture_output=True, text=True, check=False, timeout=10)
        found = done.stdout.strip()
        if done.returncode == 0 and found and not pathlib.Path(found).resolve().is_relative_to(home):
            return True
    return False


def probe_profile() -> model.Profile:
    """The sample person, plus the marker everywhere a template must not print it."""
    data = templates.sample_profile().model_dump(mode="json")
    first = data["experience"][0]
    first["notes"] = f"{SENTINEL} in a note"
    first["bullets"].append({"id": "probe-bullet", "text": f"{SENTINEL} hidden bullet", "hidden": True})
    data["experience"].append(
        {
            "id": "probe-entry",
            "title": f"{SENTINEL} hidden entry",
            "hidden": True,
            "bullets": [{"id": "probe-inner", "text": f"{SENTINEL} inside a hidden entry"}],
        }
    )
    data["skills"][0]["notes"] = f"{SENTINEL} skills note"
    # Every kind of section, because a template can honour `hidden` in one loop
    # and forget it in the next.
    for key in ("education", "projects"):
        data[key][0]["bullets"].append(
            {"id": f"probe-{key}-bullet", "text": f"{SENTINEL} {key} bullet", "hidden": True}
        )
        data[key].append({"id": f"probe-{key}", "title": f"{SENTINEL} hidden {key}", "hidden": True})
    data["skills"].append(
        {"id": "probe-skills", "category": f"{SENTINEL} skills", "items": ["x"], "hidden": True}
    )
    data["custom_sections"] = [
        {
            "id": "probe-custom",
            "title": "PROBE",
            "entries": [
                {"id": "probe-custom-shown", "title": "Shown", "bullets": [
                    {"id": "probe-custom-bullet", "text": f"{SENTINEL} custom bullet", "hidden": True}]},
                {"id": "probe-custom-hidden", "title": f"{SENTINEL} hidden custom", "hidden": True},
            ],
        }
    ]
    data["layout"].append({"key": "custom:probe-custom", "title": "PROBE"})
    return model.parse(data)


def leaks(tex: str) -> bool:
    return SENTINEL in _typeset(tex)


def missing_content(profile: model.Profile, tex: str) -> list[str]:
    return sorted(expected_words(profile) - set(words(_typeset(tex))))


def _typeset(tex: str) -> str:
    """What TeX prints: the body up to the first \\end{document}, comments removed.
    Anything after that line is never typeset, whatever it says."""
    body = latex.split_preamble(tex)[1]
    end = body.find(_END)
    return split_comments(body if end < 0 else body[:end])[0]


def expected_words(profile: model.Profile) -> set[str]:
    """Every word a reader should see, from every visible field."""
    texts = [profile.basics.name, profile.basics.headline]
    texts += [contact.label for contact in render.contacts(profile.basics)]
    for extra in profile.extras:
        texts += [extra.label, extra.value]
    for section in render.sections(profile):
        # A section whose every item is hidden is commented out whole, heading
        # included, so its title is not something a reader should see.
        if section.items and all(getattr(item, "hidden", False) for item in section.items):
            continue
        texts.append(section.title)
        for item in section.items:
            if getattr(item, "hidden", False):
                continue
            if section.kind == "summary":
                texts.append(item.text)
            elif section.kind == "skills":
                texts += [item.category, *item.items]
            elif section.kind == "languages":
                texts += [item.name, item.level, item.detail]
            elif section.key in ("experience", "education"):
                texts += [item.title, item.subtitle, item.location, item.start, item.end, item.gpa]
                texts += [bullet.text for bullet in item.bullets if not bullet.hidden]
            else:
                # Projects and custom sections: a location or link is optional.
                texts += [item.title, item.subtitle, item.start, item.end]
                texts += [bullet.text for bullet in item.bullets if not bullet.hidden]
    return set(words(" ".join(markup.rich(text) for text in texts if text)))
