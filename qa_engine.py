"""
Deterministic QA test-plan engine (no external AI).

Pipeline:
  1. normalise    - flatten Jira wiki markup / ADF into clean text
  2. sectionise   - split the ticket into Acceptance Criteria, Steps to Reproduce,
                    Expected, Actual, Out of Scope, Notes ...
  3. extract      - pull criteria from the right section only (Gherkin, bullets,
                    Jira '#' lists, 'AC1:' labels, nested bullets, then sentences)
  4. analyse      - split each criterion into actor / action / when / then
  5. derive       - run detectors (limits, required fields, formats, roles, API,
                    uploads, search, export, notifications ...) that only fire when
                    the ticket actually mentions them
  6. review       - flag vague or untestable criteria and score testability

Same input always produces the same plan.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

# =============================================================================
# 1. Normalisation
# =============================================================================

def adf_to_text(node) -> str:
    """Flatten Atlassian Document Format (REST v3 / rich custom fields) to wiki-ish text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_text(n) for n in node)
    if not isinstance(node, dict):
        return str(node)

    kind = node.get("type")
    if kind == "text":
        return node.get("text", "")
    if kind == "hardBreak":
        return "\n"
    if kind in ("bulletList", "orderedList", "taskList"):
        marker = "#" if kind == "orderedList" else "*"
        lines = []
        for child in node.get("content", []):
            text = adf_to_text(child.get("content", [])).strip()
            if text:
                first, *rest = text.splitlines()
                lines.append(f"{marker} {first}")
                lines.extend(f"{marker}{l}" if l.lstrip().startswith(("*", "#")) else l for l in rest)
        return "\n".join(lines) + "\n"
    inner = adf_to_text(node.get("content", []))
    if kind == "heading":
        return f"h{node.get('attrs', {}).get('level', 3)}. {inner}\n"
    if kind in ("paragraph", "codeBlock", "blockquote"):
        return inner + "\n"
    return inner


_WIKI_RULES = [
    (re.compile(r"\{code[^}]*\}.*?\{code\}", re.S), "\n"),             # code blocks hold payloads, not criteria
    (re.compile(r"\{noformat\}.*?\{noformat\}", re.S), "\n"),
    (re.compile(r"\{(?:color|panel|quote|expand)[^}]*\}"), ""),
    (re.compile(r"\[([^|\]\n]+)\|[^\]\n]+\]"), r"\1"),                 # [text|url] -> text
    (re.compile(r"\[(https?://[^\]\s]+)\]"), r"\1"),
    (re.compile(r"\[~[^\]]+\]"), "a user"),                            # @mentions
    (re.compile(r"\{\{([^}]+)\}\}"), r"\1"),                           # {{monospace}}
    (re.compile(r"!([^!\s]+\.(?:png|jpe?g|gif|svg|webp)[^!]*)!", re.I), ""),  # images
    (re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])"), r"\1"),  # *bold*
    (re.compile(r"(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])"), r"\1"),    # _italic_
    (re.compile(r"(?<![\w+])\+(?=\S)([^+\n]+?)(?<=\S)\+(?![\w+])"), r"\1"),  # +underline+
    (re.compile(r"\*\*(?=\S)([^*\n]+?)(?<=\S)\*\*"), r"\1"),           # markdown **bold**
    (re.compile(r"^\s*\|\|.*\|\|\s*$", re.M), ""),                     # table header rows
    (re.compile(r"&nbsp;|\u00a0"), " "),
]


def clean_markup(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    for pattern, repl in _WIKI_RULES:
        text = pattern.sub(repl, text)
    return text


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _shorten(text: str, n: int = 70) -> str:
    text = _norm(text)
    if len(text) <= n:
        return text
    cut = text[: n - 1].rsplit(" ", 1)[0]
    return cut + "…"


def _cap(s: str) -> str:
    s = _norm(s)
    return s[:1].upper() + s[1:] if s else s


def _sentence(s: str) -> str:
    s = _cap(s).rstrip()
    return s if s.endswith((".", "!", "?", ":")) else s + "."


# =============================================================================
# 2. Sections
# =============================================================================

SECTION_ALIASES = {
    "acceptance": r"acceptance\s+criteri(?:a|on)s?|ac|acs|definition\s+of\s+done|dod|"
                  r"requirements?|functional\s+requirements?|success\s+criteria|business\s+rules?",
    "scenarios": r"(?:test\s+)?scenarios?(?:\s+outline)?|use\s+cases?",
    "steps": r"steps\s+to\s+reproduce|repro(?:duction)?\s+steps|str|how\s+to\s+reproduce|steps",
    "expected": r"expected\s+(?:result|behaviou?r|outcome)s?",
    "actual": r"actual\s+(?:result|behaviou?r|outcome)s?|current\s+behaviou?r|observed\s+behaviou?r",
    "out_of_scope": r"out\s+of\s+scope|not\s+in\s+scope|non[-\s]?goals?|exclusions?",
    "environment": r"environments?|env|browsers?|devices?|platforms?",
    "notes": r"notes?|technical\s+notes?|dev\s+notes?|background|context|description|summary|"
             r"overview|problem(?:\s+statement)?|user\s+story|story|details|assumptions?|dependencies",
}

# Jira uses "# item" for numbered lists, so only "##"+ counts as a markdown heading.
_HEADING_RE = re.compile(r"^\s*(?:h[1-6]\.|#{2,6}(?=\s))\s*(.+?)\s*$", re.I)

LIST_ITEM_RE = re.compile(
    r"^(?P<indent>\s*)(?P<marker>"
    r"[*\-•+#]+(?:\s*\[[ xX]?\])?"      # *, -, •, # (Jira numbered), nested **, optional checkbox
    r"|\d+(?:\.\d+)*[.)]"                # 1.  1)  1.2.
    r"|\(?[a-z][.)]"                     # a)  (b)
    r"|\[[ xX]?\]"                       # [ ] checkbox
    r"|(?:AC|REQ|R|FR)\s*-?\s*\d+\s*[:.)-]"  # AC1:  REQ-2.
    r"|\((?:/|x|\?|!|i|on|off)\)"        # Jira icons (/) (x)
    r")\s+(?P<text>\S.*)$",
    re.I,
)


def _match_section_header(line: str):
    s = line.strip()
    heading = _HEADING_RE.match(s)
    body = heading.group(1).strip() if heading else s
    body = body.strip("*_ ").rstrip()
    if not heading and LIST_ITEM_RE.match(s) and not re.match(r"^\s*\*[^\s*]", s):
        return None
    for key, pattern in SECTION_ALIASES.items():
        m = re.match(rf"^(?:{pattern})\b\s*(?P<colon>[:\-–])?\s*(?P<rest>.*)$", body, re.I)
        if not m:
            continue
        rest = m.group("rest").strip()
        if heading or m.group("colon") or not rest:
            # "Scenario: title" must stay visible to the Gherkin parser
            return key, (body if key == "scenarios" and rest else rest)
    if heading:
        return "other", ""
    if re.match(r"^[A-Z][\w /&()'-]{2,40}:$", body):
        return "other", ""
    return None


def split_sections(text: str) -> dict:
    sections = {"preamble": []}
    current = "preamble"
    for raw in text.splitlines():
        if not raw.strip():
            continue
        header = _match_section_header(raw)
        if header:
            current, rest = header
            sections.setdefault(current, [])
            if rest:
                sections[current].append(rest)
            continue
        sections[current].append(raw.rstrip())
    return sections


# =============================================================================
# 3. Criteria extraction
# =============================================================================

@dataclass
class Criterion:
    text: str
    source: str = "list"            # gherkin | list | sentence | story
    id: str = ""
    given: list = field(default_factory=list)
    when: list = field(default_factory=list)
    then: list = field(default_factory=list)
    details: list = field(default_factory=list)
    actor: str = ""
    action: str = ""
    negative: bool = False

    def full(self) -> str:
        return " ".join([self.text, *self.given, *self.when, *self.then, *self.details])


GHERKIN_RE = re.compile(
    r"^\s*(?:[*\-#•]+\s*)?(?P<kw>scenario(?:\s+outline)?|given|when|then|and|but)\b\s*:?\s*(?P<rest>.*)$", re.I)

USER_STORY_RE = re.compile(
    r"\bas\s+an?\s+(?P<role>[^,]+?),?\s+i\s+(?:want|need|would\s+like|should\s+be\s+able)\s+(?:to\s+)?"
    r"(?P<goal>.+?)(?:,?\s+so\s+that\s+(?P<benefit>.+?))?[.]?$", re.I)

REQUIREMENT_SIGNAL = re.compile(
    r"\b(should|must|shall|will|can(?:not)?|need(?:s)?\s+to|able\s+to|allow(?:s|ed)?|display(?:s|ed)?|"
    r"show(?:s|n)?|validat\w*|prevent\w*|restrict\w*|when|if|only|required|mandatory|support\w*|enable\w*)\b",
    re.I)


def is_gherkin(lines) -> bool:
    kws = {m.group("kw").lower() for l in lines if (m := GHERKIN_RE.match(l))}
    return "then" in kws and bool(kws & {"given", "when"})


def parse_gherkin(lines):
    if not is_gherkin(lines):
        return []
    scenarios, cur, last = [], None, None
    for line in lines:
        m = GHERKIN_RE.match(line)
        if not m:
            continue
        kw, rest = m.group("kw").lower(), _norm(m.group("rest"))
        if kw.startswith("scenario"):
            cur, last = Criterion(text=rest, source="gherkin"), None
            scenarios.append(cur)
            continue
        if kw in ("and", "but"):
            kw = last or "given"
        if cur is None or (kw == "given" and (cur.when or cur.then)):
            cur = Criterion(text="", source="gherkin")
            scenarios.append(cur)
        elif kw == "when" and cur.then:  # new When after a Then = new scenario, same Given
            cur = Criterion(text="", source="gherkin", given=list(cur.given))
            scenarios.append(cur)
        getattr(cur, kw).append(rest)
        last = kw
    result = []
    for sc in scenarios:
        if not (sc.when or sc.then):
            continue
        if not sc.text:
            sc.text = sc.then[0] if sc.then else sc.when[0]
        result.append(sc)
    return result


def _list_level(indent: str, marker: str) -> int:
    run = len(re.match(r"^[*\-#•+]*", marker).group(0))
    return max(run, 1 + len(indent.replace("\t", "  ")) // 2)


_LABEL_PREFIX = re.compile(r"^(?:AC|REQ|R|FR)\s*-?\s*\d+\s*[:.)-]\s*", re.I)


def parse_list_items(lines):
    items, parent, parent_level, parent_is_group = [], None, 0, False
    gherkin = is_gherkin(lines)
    for line in lines:
        if gherkin and GHERKIN_RE.match(line):
            continue
        m = LIST_ITEM_RE.match(line)
        if not m:
            if items and line[:1] in (" ", "\t"):          # wrapped continuation line
                items[-1].text = _norm(items[-1].text + " " + line)
            continue
        text = _norm(_LABEL_PREFIX.sub("", m.group("text")))
        if len(text) < 5:
            continue
        level = _list_level(m.group("indent"), m.group("marker"))
        if parent is not None and level > parent_level:
            if parent.text.endswith(":"):
                # "Login page:" with sub-bullets -> each sub-bullet is its own criterion
                if not parent_is_group:
                    items.remove(parent)
                    parent_is_group = True
                items.append(Criterion(text=f"{parent.text.rstrip(':')} – {text}"))
            else:
                parent.details.append(text)
            continue
        c = Criterion(text=text)
        items.append(c)
        parent, parent_level, parent_is_group = c, level, False
    return items


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])|\n+")


def split_sentences(lines):
    text = "\n".join(l for l in lines if not LIST_ITEM_RE.match(l))
    out = []
    for s in _SENT_SPLIT.split(text):
        s = _norm(s).rstrip(".")
        if len(s) >= 15 and len(s.split()) >= 4:
            out.append(s)
    return out


def extract_criteria(sections: dict):
    ac_lines = sections.get("acceptance", []) + sections.get("scenarios", [])
    meta = {"steps", "expected", "actual", "out_of_scope", "environment"}
    free_lines = [l for k, v in sections.items() if k not in meta for l in v]
    pool = ac_lines or free_lines

    criteria = parse_gherkin(pool) + parse_list_items(pool)
    if not criteria:
        sentences = split_sentences(pool)
        reqs = [s for s in sentences if REQUIREMENT_SIGNAL.search(s) and not USER_STORY_RE.search(s)]
        criteria = [Criterion(text=s, source="sentence") for s in (reqs or sentences[:3])]

    story = None
    m = USER_STORY_RE.search(_norm(" ".join(free_lines)))
    if m:
        story = {k: _norm(v) if v else "" for k, v in m.groupdict().items()}
        if not criteria:
            criteria = [Criterion(text=f"{story['role']} can {story['goal']}", source="story",
                                  actor=story["role"], action=story["goal"])]

    seen, unique = set(), []
    for c in criteria:
        k = c.text.lower().rstrip(".")
        if k not in seen:
            seen.add(k)
            unique.append(c)
    for i, c in enumerate(unique, 1):
        c.id = f"AC-{i}"
    return unique, bool(ac_lines), story


# =============================================================================
# 4. Criterion analysis
# =============================================================================

_INLINE_GWT = re.compile(
    r"^(?:given\s+(?P<g>.+?),?\s+)?when\s+(?P<w>.+?),?\s+then\s+(?P<t>.+)$", re.I)
_IF_THEN = re.compile(
    r"^(?:if|when|whenever|once|after|upon|on)\s+(?P<w>[^,]+?),\s*(?:then\s+)?(?P<t>.+)$", re.I)
_MODAL = re.compile(
    r"^(?P<actor>.+?)\s+(?P<modal>should\s+be\s+able\s+to|must\s+be\s+able\s+to|should\s+not\s+be\s+able\s+to|"
    r"must\s+not|should\s+not|shouldn't|cannot|can\s+not|can't|will\s+not|won't|is\s+able\s+to|are\s+able\s+to|"
    r"should|must|shall|will|can|needs?\s+to)\s+(?P<rest>.+)$", re.I)
_NEGATIVE_MODALS = ("not", "n't", "cannot")
_USERISH = re.compile(
    r"\b(users?|customers?|admins?|administrators?|i|we|they|managers?|agents?|operators?|members?|"
    r"approvers?|reviewers?|editors?|viewers?|owners?|guests?|employees?|clients?|he|she|someone|anyone)\b", re.I)


def analyse(c: Criterion):
    if c.source == "gherkin":
        return
    t = c.text.rstrip(".")
    m = _INLINE_GWT.match(t)
    if m:
        if m.group("g"):
            c.given.append(m.group("g"))
        c.when.append(m.group("w"))
        c.then.append(m.group("t"))
        return
    m = _IF_THEN.match(t)
    if m:
        c.when.append(m.group("w"))
        c.then.append(m.group("t"))
        return
    m = _MODAL.match(t)
    if m and len(m.group("actor").split()) <= 8:
        c.actor = m.group("actor").strip()
        c.action = m.group("rest").strip()
        c.negative = any(n in m.group("modal").lower() for n in _NEGATIVE_MODALS)
        c.actor = re.sub(r"^only\s+", "", c.actor, flags=re.I)
    elif re.search(r"\b(?:is|are)\s+not\s+(?:allowed|permitted|accepted|possible)|\b(?:is|are)\s+(?:blocked|prevented|rejected|disallowed)", t, re.I):
        c.negative = True


# =============================================================================
# 5. Detectors
# =============================================================================

_LOCATION_RE = re.compile(
    r"\b(?:on|in|from|to|within|via|under)\s+(?:the\s+)?(?P<loc>(?:[\w'-]+\s+){0,3}?"
    r"(?:page|screen|tab|modal|dialog|form|section|menu|dashboard|view|panel|grid|table|widget|wizard|portal))\b",
    re.I)


def _nav_step(text: str, ctx: dict) -> str:
    for source in (text, ctx["summary"]):
        m = _LOCATION_RE.search(source)
        if m:
            return f"Navigate to the {m.group('loc')}."
    if ctx.get("components"):
        return f"Open the {ctx['components'][0]} area of the application."
    return f"Open the feature under test ({ctx['summary']})."


def _tc(key, title, ttype, priority, pre, steps, expected, covers, basis):
    return key, {
        "title": title, "type": ttype, "priority": priority,
        "preconditions": pre, "steps": steps, "expected_result": expected,
        "covers": list(covers), "basis": basis,
    }


# ---- per-criterion detectors -------------------------------------------------

_LIMIT_RE = re.compile(
    r"(?P<kind>max(?:imum)?|up\s+to|no\s+more\s+than|at\s+most|not\s+(?:to\s+)?exceed(?:ing)?|limit(?:ed)?\s+to|"
    r"min(?:imum)?|at\s+least|no\s+(?:less|fewer)\s+than)\s*(?:of\s*)?(?P<n>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>[a-zA-Z%]+)?", re.I)
_RANGE_RE = re.compile(
    r"between\s+(?P<a>\d+(?:\.\d+)?)\s*(?:and|-|to)\s*(?P<b>\d+(?:\.\d+)?)\s*(?P<unit>[a-zA-Z%]+)?", re.I)


def _step_for(n: str) -> float:
    return 0.01 if "." in n else 1


def _fmt(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:.2f}"


def detect_limits(c, ctx):
    out = []
    for m in _RANGE_RE.finditer(c.text):
        a, b = float(m.group("a")), float(m.group("b"))
        st = _step_for(m.group("a") + m.group("b"))
        unit = (m.group("unit") or "").lower()
        out.append(_tc(
            f"range:{c.id}:{a}:{b}",
            f"Boundary values for range {_fmt(a)}–{_fmt(b)} {unit}".strip(), "Boundary", "High",
            "Valid test data is available for every other field.",
            [_nav_step(c.text, ctx),
             f"Enter {_fmt(a - st)} {unit} (just below minimum) and submit.".replace("  ", " "),
             f"Enter {_fmt(a)} {unit} (minimum) and submit.".replace("  ", " "),
             f"Enter {_fmt(b)} {unit} (maximum) and submit.".replace("  ", " "),
             f"Enter {_fmt(b + st)} {unit} (just above maximum) and submit.".replace("  ", " ")],
            f"{_fmt(a)} and {_fmt(b)} are accepted; {_fmt(a - st)} and {_fmt(b + st)} are rejected with a clear message.",
            [c.id], f"{c.id}: '{m.group(0).strip()}'"))
    for m in _LIMIT_RE.finditer(c.text):
        n = float(m.group("n"))
        st = _step_for(m.group("n"))
        unit = (m.group("unit") or "").lower()
        kind = m.group("kind").lower()
        is_min = kind.startswith(("min", "at least", "no less", "no fewer"))
        if is_min:
            vals = [(n - st, "rejected"), (n, "accepted"), (n + st, "accepted")]
        else:
            vals = [(n - st, "accepted"), (n, "accepted"), (n + st, "rejected")]
        steps = [_nav_step(c.text, ctx)] + [
            f"Enter/provide {_fmt(v)} {unit} and submit.".replace("  ", " ") for v, _ in vals]
        expected = "; ".join(f"{_fmt(v)} {unit} is {res}".replace("  ", " ") for v, res in vals)
        out.append(_tc(
            f"limit:{c.id}:{kind}:{n}",
            f"Boundary: {'minimum' if is_min else 'maximum'} of {_fmt(n)} {unit}".strip(), "Boundary", "High",
            "Valid test data is available for every other field.", steps,
            expected + ". Rejections show a clear validation message and nothing is saved.",
            [c.id], f"{c.id}: '{m.group(0).strip()}'"))
    return out


_REQUIRED_RE = re.compile(
    r"(?P<field>[\w][\w /-]{0,40}?)\s+(?:field\s+|fields\s+)?(?:is|are)\s+(?:a\s+)?(?:required|mandatory|compulsory)"
    r"|(?:required|mandatory)\s+fields?\s*(?:are|is|:)?\s*(?P<field2>[\w ,/-]{2,60})", re.I)


def detect_required(c, ctx):
    m = _REQUIRED_RE.search(c.text)
    if not m:
        return []
    fld = _norm(m.group("field") or m.group("field2") or "required field")
    fld = re.sub(r"^(?:the|a|an|all)\s+", "", fld, flags=re.I)
    return [_tc(
        f"required:{fld.lower()}", f"Mandatory check: {fld} left empty", "Negative", "High",
        "Form is open with all other fields filled with valid data.",
        [_nav_step(c.text, ctx), f"Leave {fld} empty and submit.",
         f"Enter only spaces in {fld} and submit.", f"Fill {fld} with a valid value and submit."],
        f"Empty / whitespace-only {fld} is rejected with a field-level message and nothing is saved; "
        f"a valid value is accepted.",
        [c.id], f"{c.id}: required field")]


_FORMATS = {
    "email address": (r"\be-?mail\s+(?:address|field|format|id)\b|\bvalid\s+e-?mail|\benter\s+(?:an?\s+|their\s+|your\s+)?e-?mail",
                      ["plainaddress", "user@", "@domain.com", "user@domain", "user name@domain.com",
                       "user+qa@domain.com (valid – should be accepted)"]),
    "phone number": (r"\bphone(?:\s+number)?\b|\bmobile\s+(?:number|no)\b|\bcontact\s+number",
                     ["letters (abc123)", "too few digits", "too many digits", "with country code",
                      "with spaces / dashes"]),
    "date": (r"\bdate\s+(?:field|format|picker)\b|\bdate\s+of\s+birth\b|\bdob\b|dd/mm|mm/dd|yyyy|\bstart\s+date\b|\bend\s+date\b",
             ["31/02 (invalid day)", "29/02 on a non-leap year", "end date before start date",
              "far past (01/01/1900)", "far future", "wrong format (2025-13-01)"]),
    "URL": (r"\burl\b|\bweb\s*site\b|\bhyperlink\b",
            ["missing scheme (www.example.com)", "htp://typo", "javascript:alert(1)", "very long URL (2,000+ chars)"]),
    "password": (r"\bpassword\b",
                 ["shorter than the minimum length", "missing a required character class", "same as username",
                  "leading/trailing spaces", "password visible in page source / logs (must not be)"]),
    "number": (r"\bnumeric\b|\bnumbers?\s+only\b|\binteger\b|\bdecimal\b|\bamount\b|\bquantity\b|\bprice\b|\bpercentage\b",
                      ["negative number", "zero", "decimals where whole numbers are expected",
                       "letters / symbols", "very large number (9999999999999)"]),
}


def detect_formats(c, ctx):
    out = []
    for name, (pattern, cases) in _FORMATS.items():
        if re.search(pattern, c.text, re.I):
            out.append(_tc(
                f"format:{name}", f"Format validation: invalid {name} values", "Negative", "Medium",
                "Form is open with all other fields valid.",
                [_nav_step(c.text, ctx)] + [f"Enter {v}, submit, and note the result." for v in cases],
                f"Each invalid {name} is rejected with a specific message; valid values are accepted and saved.",
                [c.id], f"{c.id}: mentions {name}"))
    return out


_ROLE_RE = re.compile(
    r"\b(super\s?admins?|admin(?:istrator)?s?|managers?|approvers?|reviewers?|viewers?|editors?|owners?|guests?|"
    r"read[- ]only\s+users?|authors?|agents?|supervisors?|team\s+leads?|operators?|auditors?|"
    r"(?:non|un)[- ]?(?:admin|authori[sz]ed|privileged)\s+users?)\b", re.I)
_PERM_RE = re.compile(
    r"\bonly\b.{0,40}\b(?:can|should|may|is|are|allowed|able)\b|\bpermissions?\b|\broles?\b|\baccess\b|"
    r"\bauthori[sz]\w*|\brestricted\b|\bprivileg\w*|\bnot\s+(?:be\s+)?visible\s+to\b|\bread[- ]only\b", re.I)


def detect_permissions(c, ctx):
    if not _PERM_RE.search(c.text):
        return []
    roles = sorted({_norm(r).lower() for r in _ROLE_RE.findall(c.text)})
    action = c.action or c.text
    if c.negative or re.search(r"\bread[- ]only\b|\bcannot\b|\bnot\s+(?:be\s+)?(?:able|allowed|visible)", c.text, re.I):
        who = ", ".join(roles) if roles else "the restricted role"
        return [_tc(
            f"perm-restrict:{c.id}", f"Access: {who} is restricted as specified", "Security", "High",
            f"Test account(s) exist for: {who}.",
            [f"Log in as {who}.", _nav_step(c.text, ctx), "Try to view, create, edit and delete in this area.",
             "Repeat the blocked actions by direct URL and by replaying the API request."],
            f"Only what the criterion allows is possible: {_sentence(c.text)} Blocked actions are hidden/disabled "
            "in the UI and return 403 from the API.", [c.id], f"{c.id}: access rule")]
    allowed = ", ".join(roles) if roles else "the permitted role"
    return [
        _tc(f"perm-allow:{c.id}", f"Access: {allowed} can perform the action", "Security", "High",
            f"Test accounts exist for: {allowed}.",
            [f"Log in as {allowed}.", _nav_step(c.text, ctx), f"Perform: {_shorten(action, 120)}."],
            "The action is available and completes successfully.", [c.id], f"{c.id}: access rule"),
        _tc(f"perm-deny:{c.id}", "Access: unauthorised role is blocked (UI and direct URL/API)", "Security", "High",
            "A test account WITHOUT the required role exists.",
            ["Log in with a role that should not have access.", _nav_step(c.text, ctx),
             "Confirm the control/menu is hidden or disabled.",
             "Open the feature by direct URL, and replay the underlying API request with this user's token."],
            "UI does not expose the action; direct URL shows an access-denied page; API returns 403. No data changes.",
            [c.id], f"{c.id}: access rule"),
    ]


_UPLOAD_RE = re.compile(r"\bupload\w*|\battach(?:ment|ments|es|ed)?\b|\bimport\w*\b|\bdrag\s+and\s+drop\b", re.I)
_FILETYPE_RE = re.compile(r"\b(pdf|csv|xlsx?|docx?|pptx?|png|jpe?g|gif|txt|json|xml|zip|mp4|svg)\b", re.I)


def detect_upload(c, ctx):
    if not _UPLOAD_RE.search(c.text):
        return []
    types = sorted({t.upper() for t in _FILETYPE_RE.findall(c.text)})
    allowed = ", ".join(types) if types else "each supported file type"
    return [_tc(
        f"upload:{c.id}", "File upload: allowed, disallowed and malformed files", "Negative", "High",
        "Sample files prepared: valid, wrong type, renamed extension, 0-byte, oversize, unicode file name.",
        [_nav_step(c.text, ctx), f"Upload a valid file of {allowed}.",
         "Upload a disallowed type (e.g. .exe) and a .exe renamed to an allowed extension.",
         "Upload a 0-byte file and a file above the size limit.",
         "Upload a file whose name has spaces, unicode and special characters.",
         "Upload the same file twice."],
        f"{allowed} accepted; disallowed, disguised, empty and oversize files rejected with clear messages; "
        "file names handled safely; duplicate behaviour matches spec.",
        [c.id], f"{c.id}: file upload")]


_NOTIFY_RE = re.compile(
    r"\bnotif(?:y|ies|ied|ication|ications)\b|\b(?:e-?mail|sms|alert|message)s?\s+(?:is\s+|are\s+|will\s+be\s+)?sent\b|"
    r"\bsends?\s+(?:an?\s+)?(?:e-?mail|sms|alert|notification|message)\b|\breminder\w*", re.I)


def detect_notifications(c, ctx):
    if not _NOTIFY_RE.search(c.text):
        return []
    return [_tc(
        f"notify:{c.id}", "Notification: right recipients, content, and no duplicates", "Positive", "Medium",
        "Access to the test mailbox / SMS sink / notification log.",
        [_nav_step(c.text, ctx), "Perform the triggering action once.",
         "Check the notification recipients, subject and body (placeholders filled, links work).",
         "Repeat the action quickly twice.", "Make the action fail (e.g. validation error)."],
        "Exactly one notification per successful action, to the correct recipients, with correct content; "
        "none sent when the action fails.",
        [c.id], f"{c.id}: notification")]


_DELETE_RE = re.compile(r"\b(delete|deletes|deleted|deleting|remove|removes|removed|archive|archived)\b", re.I)


def detect_delete(c, ctx):
    if not _DELETE_RE.search(c.text):
        return []
    return [_tc(
        f"delete:{c.id}", "Delete/remove: confirmation, cancel, and dependent data", "Edge Case", "Medium",
        "A record exists, including one that is referenced by other records.",
        [_nav_step(c.text, ctx), "Start the delete and cancel the confirmation.",
         "Delete again and confirm.", "Refresh, search for the item, and open its old URL.",
         "Try to delete the record that other data depends on."],
        "Cancel keeps the record; confirm removes it everywhere (list, search, URL); dependent-record case is "
        "blocked or handled as specified.",
        [c.id], f"{c.id}: delete/remove")]


_UNIQUE_RE = re.compile(r"\bunique\b|\bduplicat\w*|\balready\s+exists?\b", re.I)


def detect_unique(c, ctx):
    if not _UNIQUE_RE.search(c.text):
        return []
    return [_tc(
        f"unique:{c.id}", "Uniqueness: duplicate entries are rejected", "Negative", "Medium",
        "A record with a known value already exists.",
        [_nav_step(c.text, ctx), "Create a new record with exactly the same value.",
         "Repeat with different letter case and with leading/trailing spaces."],
        "Duplicates are rejected with a clear message (case/whitespace handling as specified).",
        [c.id], f"{c.id}: uniqueness")]


_PERF_RE = re.compile(
    r"\bwithin\s+(?P<n>\d+(?:\.\d+)?)\s*(?P<u>ms|milliseconds?|s|secs?|seconds?|minutes?|mins?)\b|"
    r"\b(?:under|less\s+than|<)\s*(?P<n2>\d+(?:\.\d+)?)\s*(?P<u2>ms|milliseconds?|s|secs?|seconds?)\b", re.I)


def detect_performance(c, ctx):
    m = _PERF_RE.search(c.text)
    if not m:
        return []
    limit = f"{m.group('n') or m.group('n2')} {m.group('u') or m.group('u2')}"
    return [_tc(
        f"perf:{c.id}", f"Performance: completes within {limit}", "Performance", "Medium",
        "Production-like data volume; browser dev tools or APM available.",
        [_nav_step(c.text, ctx), "Perform the action 10 times and record each duration.",
         "Repeat with the largest realistic data set."],
        f"Every run (or the agreed percentile, e.g. p95) completes within {limit}.",
        [c.id], f"{c.id}: time limit")]


_STATUS_RE = re.compile(r"\bstatus\b|\bstates?\b|\bworkflow\b|\btransition\w*|\bapprov\w+|\breject\w*", re.I)


def detect_status(c, ctx):
    if not _STATUS_RE.search(c.text):
        return []
    return [_tc(
        f"status:{c.id}", "Workflow: valid and invalid status transitions", "Edge Case", "Medium",
        "Records exist in each relevant status.",
        [_nav_step(c.text, ctx), "Move a record through each allowed transition.",
         "Attempt a transition that should not be allowed (UI and API).",
         "Check history/audit after each change."],
        "Allowed transitions succeed and are recorded; disallowed ones are blocked.",
        [c.id], f"{c.id}: status/workflow")]


PER_CRITERION = [detect_limits, detect_required, detect_formats, detect_permissions, detect_upload,
                 detect_notifications, detect_delete, detect_unique, detect_performance, detect_status]


# ---- ticket-level detectors --------------------------------------------------

def _covers(criteria, pattern):
    return [c.id for c in criteria if re.search(pattern, c.full(), re.I)]


_API_RE = r"\bapi\b|\bendpoints?\b|\brest\b|\bgraphql\b|\bstatus\s+code|\bjson\b|\bpayload\b|\bwebhook|\b(?:GET|POST|PUT|PATCH|DELETE)\s+/"
_ENDPOINT_RE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/[\w/{}\-:.]*)")
_CODE_RE = re.compile(r"\b([1-5]\d{2})\b")
_UI_FORM_RE = r"\b(forms?|submit\w*|save[sd]?|input\s+fields?|text\s+fields?|fields?|enters?|fill\s+in|creat\w+|edit\w*|updat\w+|register\w*)\b"
_SEARCH_RE = r"\bsearch\w*|\bfilter\w*|\bsort\w*|\bpaginat\w*"
_EXPORT_RE = r"\bexport\w*|\bdownload\w*|\breports?\b"
_AUTH_RE = r"\blog\s?in\b|\bsign\s?in\b|\bauthenticat\w*|\bsso\b|\botp\b|\b2fa\b|\bmfa\b|\bsession\b|\blog\s?out\b"
_I18N_RE = r"\blocali[sz]\w*|\btranslat\w*|\blanguages?\b|\bi18n\b|\bcurrenc\w*|\brtl\b"
_AUDIT_RE = r"\baudit\w*|\bhistory\b|\blogged\b|\bactivity\s+log"
_TIME_RE = r"\btime\s?zones?\b|\butc\b|\bdeadline|\bexpir\w*|\bschedul\w*|\bdue\s+date|\bcron\b|\bdaylight"


def ticket_level_tests(criteria, full_text, ctx):
    out = []
    if re.search(_API_RE, full_text, re.I):
        cov = _covers(criteria, _API_RE)
        endpoints = _ENDPOINT_RE.findall(full_text)
        codes = sorted(set(_CODE_RE.findall(full_text)) - {"100"}) if re.search(r"status|code|return|respond", full_text, re.I) else []
        targets = [f"{m} {p}" for m, p in endpoints] or ["the endpoint(s) in this ticket"]
        for t in targets[:6]:
            out.append(_tc(
                f"api-contract:{t}", f"API contract: {t}", "Positive", "High",
                "Valid auth token; API client (Postman/curl) ready.",
                [f"Send a valid request to {t}.", "Validate status code, headers and response schema/field types.",
                 "Verify the data is persisted/returned correctly (DB or follow-up GET)."],
                "Response matches the documented contract" + (f" (expected codes mentioned: {', '.join(codes)})" if codes else "") + ".",
                cov, "API mentioned"))
        out.append(_tc(
            "api-negative", "API negative: auth, malformed and incomplete requests", "Negative", "High",
            "API client ready; one valid and one expired token.",
            ["Call without a token, then with an expired/invalid token.",
             "Send malformed JSON.", "Omit each required field; send wrong data types; send unknown fields.",
             "For PUT/DELETE, repeat the same request twice (idempotency)."],
            "401 for missing/invalid auth; 400/422 with a helpful error body for bad input; no 500s; "
            "repeat calls do not create duplicates.", cov, "API mentioned"))

    has_form = re.search(_UI_FORM_RE, full_text, re.I)
    if has_form:
        cov = _covers(criteria, _UI_FORM_RE)
        out.append(_tc(
            "input-security", "Input safety: special characters, unicode, script and SQL strings", "Security", "Medium",
            "Form/input is accessible.",
            [_nav_step(full_text, ctx), "Enter unicode (é, 漢字, emoji) and save; reopen the record.",
             "Enter <script>alert(1)</script> and ' OR '1'='1 in text inputs and save.",
             "Enter the maximum allowed length, then one character more."],
            "Unicode is stored and displayed correctly; script/SQL strings are stored as plain text and never executed; "
            "length limits enforced.", cov, "data entry detected"))
        out.append(_tc(
            "double-submit", "Resilience: double submit and network drop", "Edge Case", "Medium",
            "Browser dev tools available (network throttling/offline).",
            [_nav_step(full_text, ctx), "Click the submit/save action twice quickly.",
             "Throttle to slow 3G, submit, then go offline mid-request; restore the connection."],
            "Only one record is created; the user gets a clear error and can retry without losing entered data.",
            cov, "data entry detected"))

    if re.search(_SEARCH_RE, full_text, re.I):
        cov = _covers(criteria, _SEARCH_RE)
        found = {k for k in ("search", "filter", "sort", "paginat") if re.search(rf"\b{k}", full_text, re.I)}
        sub = {
            "search": "Search with no results, partial text, different letter case, leading/trailing spaces "
                      "and special characters (%, _, ', &).",
            "filter": "Apply each filter alone, combine filters, then clear them; include a filter value with no data.",
            "sort": "Sort ascending and descending, including empty values and ties.",
            "paginat": "Go to the first, last and an out-of-range page; change page size.",
        }
        names = "/".join(k if k != "paginat" else "pagination" for k in sub if k in found)
        out.append(_tc(
            "search", f"{names.capitalize()}: empty, partial and edge-case behaviour", "Edge Case", "Medium",
            "Test data includes matches, near-matches and no-matches.",
            [_nav_step(full_text, ctx)] + [v for k, v in sub.items() if k in found],
            "Results are correct for each case, the empty state is helpful, and ordering/paging is stable.",
            cov, f"{names} mentioned"))

    if re.search(_EXPORT_RE, full_text, re.I):
        cov = _covers(criteria, _EXPORT_RE)
        out.append(_tc(
            "export", "Export/report: content matches screen, encoding and empty data", "Positive", "Medium",
            "Data with unicode, commas, quotes and line breaks exists.",
            [_nav_step(full_text, ctx), "Apply filters and export.", "Open the file in Excel/text editor.",
             "Export with no matching data.", "Export the largest realistic data set."],
            "File rows/columns match the filtered on-screen data; special characters intact; empty export is valid; "
            "large export completes.", cov, "export/report mentioned"))

    if re.search(_AUTH_RE, full_text, re.I):
        cov = _covers(criteria, _AUTH_RE)
        out.append(_tc(
            "auth", "Authentication: invalid credentials, lockout and session handling", "Security", "High",
            "Valid and invalid test accounts.",
            ["Log in with a wrong password and with an unknown username.",
             "Exceed the failed-attempt limit (if defined).", "Log out, then press Back and reuse the old session.",
             "Leave the session idle past the timeout."],
            "Same generic error for wrong user/password (no account enumeration); lockout as specified; "
            "old sessions are invalid after logout/timeout.", cov, "authentication mentioned"))

    if re.search(_TIME_RE, full_text, re.I):
        cov = _covers(criteria, _TIME_RE)
        out.append(_tc(
            "time", "Date/time: time zones, DST and exact expiry boundary", "Edge Case", "Medium",
            "Users/test machines in two different time zones.",
            ["Create the time-based item from both time zones.", "Check values right at the expiry/deadline moment "
             "(one minute before, exactly at, one minute after).", "Check a date across a daylight-saving change."],
            "Times display in the correct zone and boundary behaviour matches the spec exactly.",
            cov, "time-based logic mentioned"))

    if re.search(_I18N_RE, full_text, re.I):
        out.append(_tc(
            "i18n", "Localisation: translations, formats and long strings", "UI", "Low",
            "Application can be switched between supported languages/locales.",
            ["Switch to each supported language.", "Check date, number and currency formats.",
             "Check layout with the longest translations."],
            "All text translated; formats follow the locale; no truncation or overflow.",
            _covers(criteria, _I18N_RE), "localisation mentioned"))

    if re.search(_AUDIT_RE, full_text, re.I):
        out.append(_tc(
            "audit", "Audit trail: who, what and when is recorded", "Positive", "Medium",
            "Access to the audit/history view or log.",
            ["Perform create, update and delete actions.", "Open the audit/history view."],
            "Each action is recorded with user, timestamp and before/after values.",
            _covers(criteria, _AUDIT_RE), "audit/history mentioned"))
    return out


def bug_tests(sections, ctx, criteria):
    steps_lines = sections.get("steps", [])
    steps = []
    for line in steps_lines:
        m = LIST_ITEM_RE.match(line)
        steps.append(_sentence(m.group("text") if m else line))
    expected = _norm(" ".join(sections.get("expected", [])))
    actual = _norm(" ".join(sections.get("actual", [])))
    env = _norm(" ".join(sections.get("environment", [])))
    cov = [c.id for c in criteria]
    out = [_tc(
        "bug-repro", "Defect fix: original reproduction steps no longer fail", "Regression", "Critical",
        f"Build containing the fix is deployed{f' ({env})' if env else ''}; same data/role as the reporter.",
        steps or ["Follow the reproduction steps from the ticket (none were found – ask the reporter)."],
        (_sentence(expected) if expected else "The behaviour described as 'expected' in the ticket occurs.")
        + (f" The reported problem ('{_shorten(actual, 100)}') no longer occurs." if actual else ""),
        cov, "Bug ticket: steps to reproduce")]
    out.append(_tc(
        "bug-variants", "Defect fix: variations of the failing scenario", "Regression", "High",
        "Fix deployed.",
        ["Repeat the reproduction with different data (empty, max length, special characters).",
         "Repeat with a different user role and a different browser/device.",
         "Repeat the steps through any other entry point (API, import, different screen)."],
        "The defect does not occur in any variation.", cov, "Bug ticket"))
    out.append(_tc(
        "bug-regression", "Regression: neighbouring functionality still works", "Regression", "High",
        "Fix deployed; list of features sharing the changed code (ask the developer).",
        ["Run the main happy path of the affected feature end to end.",
         "Run smoke tests for features that share the changed component/API."],
        "No new defects in related areas.", cov, "Bug ticket"))
    return out


# =============================================================================
# 6. Review: ambiguity and testability
# =============================================================================

VAGUE_TERMS = [
    "fast", "quickly", "quick", "user-friendly", "user friendly", "intuitive", "easy", "easily", "simple",
    "appropriate", "appropriately", "properly", "correctly", "as expected", "as needed", "etc", "and so on",
    "reasonable", "adequate", "efficient", "robust", "seamless", "seamlessly", "flexible", "optimal",
    "improved", "better", "similar", "tbd", "tba", "to be decided", "if possible", "where applicable",
    "normal", "works fine", "nice", "good", "relevant", "various", "and/or", "accordingly",
]
_VAGUE_RE = re.compile(r"\b(" + "|".join(re.escape(t) for t in VAGUE_TERMS) + r")\b", re.I)


def review(criteria, has_ac_section, sections, issue_type, full_text, derived_ids=()):
    findings = []
    if not full_text.strip() or not any(sections.values()):
        findings.append("Ticket has no description – only generic tests could be produced.")
    elif not has_ac_section and not (issue_type.lower() in ("bug", "defect") or sections.get("steps")):
        findings.append("No 'Acceptance Criteria' section found – criteria were inferred from free text. "
                        "Add an AC section to the ticket for more accurate tests.")
    if issue_type.lower() == "bug" and not sections.get("steps"):
        findings.append("Bug ticket has no 'Steps to Reproduce' section.")
    if issue_type.lower() == "bug" and not sections.get("expected"):
        findings.append("Bug ticket has no 'Expected Result' section.")

    vague_count, untestable = 0, 0
    for c in criteria:
        terms = sorted({t.lower() for t in _VAGUE_RE.findall(c.text)})
        if terms:
            vague_count += 1
            findings.append(f"{c.id} uses vague wording ({', '.join(repr(t) for t in terms)}) – ask for a measurable value.")
        if not (c.then or c.action or c.negative or c.id in derived_ids or REQUIREMENT_SIGNAL.search(c.text)) \
                and c.source != "gherkin":
            untestable += 1
            findings.append(f"{c.id} has no clear expected outcome – confirm what 'done' looks like.")

    if criteria and not re.search(r"\berror|invalid|fail\w*|reject\w*|not\s+allowed|cannot|must\s+not|warning\b|\b4\d\d\b|expired",
                                  full_text, re.I):
        findings.append("No error/negative behaviour is specified – expected error messages in negative tests are assumptions.")

    if issue_type.lower() in ("bug", "defect") or sections.get("steps"):
        score = (40 if sections.get("steps") else 0) + (30 if sections.get("expected") else 0) \
              + (20 if sections.get("actual") else 0) + (10 if sections.get("environment") else 0)
        return findings, score

    score = 0
    score += 30 if has_ac_section else 0
    score += 20 if criteria else 0
    if criteria:
        score += round(30 * (len(criteria) - untestable) / len(criteria))
        score += round(20 * (len(criteria) - vague_count) / len(criteria))
    return findings, min(score, 100)


# =============================================================================
# Public API
# =============================================================================

PRIORITY_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}


def _lower_first(s: str) -> str:
    first = s.split(" ", 1)[0]
    return s[:1].lower() + s[1:] if first in ("The", "A", "An", "It", "This", "That", "Each", "Every", "All") else s


def _singular(actor: str) -> str:
    a = re.sub(r"^(?:all|any|only)\s+", "", actor.strip(), flags=re.I)
    if re.fullmatch(r"[A-Za-z]+s", a) and not a.lower().endswith("ss"):
        a = a[:-1]
        a = ("an " if a[0].lower() in "aeio" else "a ") + a
    return a


def _criterion_test(c: Criterion, ctx, story):
    pre = []
    actor = _singular(c.actor) if c.actor and _USERISH.search(c.actor) else ""
    role = actor if actor and not re.fullmatch(r"(?:the\s+)?(?:an?\s+)?users?|i|we", actor, re.I) \
        else (story or {}).get("role")
    pre.append(f"Acting as {role}; valid test data exists." if role else
               "Logged in as a user with the permissions this feature needs; valid test data exists.")
    pre += [_sentence(g) for g in c.given]

    steps = [_nav_step(c.text, ctx)]
    if c.when and c.source == "gherkin":
        steps += [_sentence(w) for w in c.when]
    elif c.when:
        steps += [_sentence(f"Trigger the condition: {w}") for w in c.when]
    elif c.action and _USERISH.search(c.actor):
        verb = "Attempt to" if c.negative else "As " + _singular(c.actor).lower() + ","
        steps.append(_sentence(f"{verb} {c.action}"))
    elif c.negative:
        steps.append(_sentence(f"Attempt the blocked behaviour: {c.text}"))
    elif c.action:
        steps.append(_sentence(f"Perform the steps that lead to: {_lower_first(c.text)}"))
    else:
        steps.append(_sentence(f"Exercise the behaviour: {c.text}"))
    steps.append("Observe the result (UI and, if relevant, stored data).")

    if c.then:
        expected = " ".join(_sentence(t) for t in c.then)
    elif c.negative:
        expected = _sentence(c.text) + " The action is blocked or not offered, with a clear message if attempted."
    else:
        expected = _sentence(c.text)
    if c.details:
        expected += " Also verify: " + "; ".join(c.details) + "."

    return _tc(f"ac:{c.id}", f"{c.id}: {_shorten(c.text)}", "Negative" if c.negative else "Positive",
               "High", " ".join(pre), steps, expected, [c.id], f"{c.id} ({c.source})")


def generate_test_plan(ticket: dict) -> dict:
    """ticket keys: key, summary, description, issue_type, [components, labels, priority]."""
    raw = ticket.get("description") or ""
    text = clean_markup(adf_to_text(raw) if not isinstance(raw, str) else raw)
    sections = split_sections(text)
    criteria, has_ac, story = extract_criteria(sections)
    for c in criteria:
        analyse(c)

    ctx = {"summary": ticket.get("summary", ""), "components": ticket.get("components") or []}
    full_text = ticket.get("summary", "") + "\n" + text
    # Detection ignores out-of-scope items, and for bugs ignores the navigation-only repro steps
    skip = {"out_of_scope", "steps", "environment"}
    detect_text = ticket.get("summary", "") + "\n" + "\n".join(
        l for k, v in sections.items() if k not in skip for l in v)
    issue_type = ticket.get("issue_type", "")

    ordered = {}

    def add(pairs):
        for key, tc in pairs:
            if key in ordered:
                ordered[key]["covers"] = sorted(set(ordered[key]["covers"]) | set(tc["covers"]))
            else:
                ordered[key] = tc

    is_bug = issue_type.lower() in ("bug", "defect") or bool(sections.get("steps"))
    if is_bug:
        add(bug_tests(sections, ctx, criteria))
    derived = {}
    for c in criteria:
        view = Criterion(text=c.full(), id=c.id, action=c.action, negative=c.negative)
        derived[c.id] = [pair for det in PER_CRITERION for pair in det(view, ctx)]
    for c in criteria:
        # A pure constraint ("Name must be max 50 chars", "Email is required") is better
        # covered by its specific detector test than by a vague "verify X" case.
        is_constraint = not c.when and not c.details and not (c.actor and _USERISH.search(c.actor))
        if not (is_constraint and derived[c.id]):
            add([_criterion_test(c, ctx, story)])
    for c in criteria:
        add(derived[c.id])
    add(ticket_level_tests(criteria, detect_text, ctx))

    if len(ordered) <= len(criteria) and not is_bug:   # nothing specific detected
        add([_tc("generic-negative", "Generic negative: invalid, empty and unexpected input", "Negative", "Medium",
                 "Feature is accessible.",
                 [_nav_step(full_text, ctx), "Try the main action with missing, empty and invalid data.",
                  "Try it in an unexpected order (e.g. refresh mid-flow, browser Back)."],
                 "The system rejects bad input with clear messages and never ends in an inconsistent state.",
                 [c.id for c in criteria], "Generic heuristic (ticket gave no specifics)")])

    test_cases = list(ordered.values())
    for i, tc in enumerate(test_cases, 1):
        tc["id"] = f"TC-{i:03d}"

    findings, score = review(criteria, has_ac, sections, issue_type, text,
                             {cid for cid, pairs in derived.items() if pairs})

    trace = {c.id: [tc["id"] for tc in test_cases if c.id in tc["covers"]] for c in criteria}
    types = {tc["type"] for tc in test_cases}

    scope_in = [f"{c.id}: {_shorten(c.text, 90)}" for c in criteria]
    for label, cond in [("Boundary values", "Boundary" in types), ("Role-based access", "Security" in types),
                        ("API contract and error handling", bool(re.search(_API_RE, detect_text, re.I))),
                        ("Regression around the fix", is_bug)]:
        if cond:
            scope_in.append(label)
    scope_out = [_norm(LIST_ITEM_RE.match(l).group("text")) if LIST_ITEM_RE.match(l) else _norm(l)
                 for l in sections.get("out_of_scope", [])]
    if "Performance" not in types:
        scope_out.append("Load/performance testing (no performance criteria in ticket)")
    scope_out.append("Third-party/upstream service failures beyond basic error handling")

    return {
        "summary": f"Test plan for {ticket.get('key', '')}: {ticket.get('summary', '')}",
        "criteria": [{"id": c.id, "text": c.text, "source": c.source, "details": c.details} for c in criteria],
        "user_story": story,
        "scope_in": scope_in,
        "scope_out": scope_out,
        "risks": findings,
        "testability_score": score,
        "traceability": trace,
        "test_cases": test_cases,
        "generator": "Rules",
    }


# =============================================================================
# Exporters
# =============================================================================

def to_markdown(plan: dict, key: str, cases=None) -> str:
    cases = plan["test_cases"] if cases is None else cases
    out = [f"# Test Plan for {key}", "", f"**Overview:** {plan['summary']}",
           f"**Testability score:** {plan['testability_score']}/100", "", "## Acceptance criteria"]
    out += [f"- **{c['id']}** {c['text']}" for c in plan["criteria"]] or ["- (none found)"]
    out += ["", "## In scope"] + [f"- {s}" for s in plan["scope_in"]]
    out += ["", "## Out of scope"] + [f"- {s}" for s in plan["scope_out"]]
    if plan["risks"]:
        out += ["", "## Risks & open questions"] + [f"- {r}" for r in plan["risks"]]
    out += ["", "## Test cases", ""]
    for tc in cases:
        out += [f"### [{tc['id']}] {tc['title']}",
                f"- **Type:** {tc['type']} | **Priority:** {tc['priority']} | **Covers:** {', '.join(tc['covers']) or '-'}",
                f"- **Preconditions:** {tc['preconditions']}"]
        if tc.get("test_data"):
            out.append(f"- **Test data:** {tc['test_data']}")
        out.append("- **Steps:**")
        out += [f"  {i}. {s}" for i, s in enumerate(tc["steps"], 1)]
        out += [f"- **Expected result:** {tc['expected_result']}", ""]
    return "\n".join(out)


def _wiki(s: str) -> str:
    return re.sub(r"([{}\[\]|])", r"\\\1", s)


def to_jira_wiki(plan: dict, key: str, cases=None, limit: int = 32000) -> str:
    """Jira Server/Cloud REST v2 comments use wiki markup, not Markdown."""
    cases = plan["test_cases"] if cases is None else cases
    out = [f"h2. Test Plan for {key}", f"*Overview:* {_wiki(plan['summary'])}",
           f"*Testability score:* {plan['testability_score']}/100", "", "h3. Acceptance criteria"]
    out += [f"* *{c['id']}* {_wiki(c['text'])}" for c in plan["criteria"]] or ["* (none found)"]
    if plan["risks"]:
        out += ["", "h3. Risks & open questions"] + [f"* {_wiki(r)}" for r in plan["risks"]]
    out += ["", "h3. Summary", "||ID||Title||Type||Priority||Covers||"]
    out += [f"|{tc['id']}|{_wiki(tc['title'])}|{tc['type']}|{tc['priority']}|{', '.join(tc['covers']) or '-'}|"
            for tc in cases]
    out += ["", "h3. Test cases"]
    for tc in cases:
        block = [f"h4. {tc['id']} – {_wiki(tc['title'])}",
                 f"*Type:* {tc['type']} | *Priority:* {tc['priority']} | *Covers:* {', '.join(tc['covers']) or '-'}",
                 f"*Preconditions:* {_wiki(tc['preconditions'])}"]
        if tc.get("test_data"):
            block.append(f"*Test data:* {_wiki(tc['test_data'])}")
        block += [f"# {_wiki(s)}" for s in tc["steps"]]
        block += [f"*Expected:* {_wiki(tc['expected_result'])}", ""]
        if len("\n".join(out + block)) > limit - 200:
            out.append("_Comment truncated – download the full plan from the generator._")
            break
        out += block
    return "\n".join(out)


def to_csv(cases) -> str:
    """Flat CSV that imports into Xray / Zephyr / TestRail with a column mapping."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ID", "Title", "Type", "Priority", "Covers", "Preconditions", "Test Data", "Steps", "Expected Result"])
    for tc in cases:
        w.writerow([tc["id"], tc["title"], tc["type"], tc["priority"], ", ".join(tc["covers"]),
                    tc["preconditions"], tc.get("test_data", ""), "\n".join(f"{i}. {s}" for i, s in enumerate(tc["steps"], 1)),
                    tc["expected_result"]])
    return buf.getvalue()
