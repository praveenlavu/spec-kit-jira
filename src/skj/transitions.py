"""Which Jira workflow transition a write will fire. It never guesses (K3).

`choose(transitions, current_status_id, want, names)` takes the `transitions` list of `GET /issue/{key}/transitions`
(untrusted server data) and returns the one transition to POST, or raises `TransitionRefused` with a fixed message.
The wanted state is read from the status category of the transition's target only (`indeterminate` for "active",
`done` for "done"); nothing else on the issue or the transition says whether it is done.

Returns a minimal validated dict `{"id", "name", "to_id", "to_name"}`: `id` and `to_id` are digit strings that passed
`^[1-9][0-9]{0,9}$`; `name` and `to_name` are for display only (control, format and bidi characters stripped, 80
characters at most). W3 posts `id` and shows `id` and `name` in its preview.

Refusal codes (`TransitionRefused.code`), each with one fixed message in `MESSAGES`:
  none             no candidate (or no transition with the configured name, or an empty/non-str name)
  ambiguous        two or more candidates or name matches, or a duplicate transition id in the list
  screen           the chosen transition has `hasScreen: true`
  conditional      the chosen transition has `isConditional: true`
  required-fields  the chosen transition has a `fields` map in which any entry is not exactly `required: false`
  unavailable      `isAvailable: false`, AND any malformed input (not a list, more than MAX_TRANSITIONS entries, any
                   malformed entry, a bad current status id): the list cannot be trusted, so nothing is picked from it.
  not-done         `choose_done` only: a configured `transitions.done` name picks a transition whose target is not in the
                   done category (H1a; `choose` itself lets a name win over the category)

Category mode counts candidates over the whole list, current status included (the verified list includes the current
state): two or more -> ambiguous from every state, so team-managed `active` always needs `transitions.active`; exactly
one that targets the current status -> none (already there; W3 maps that to a no-op). Name mode excludes transitions that
only reach the current status. A configured name must be a non-empty str, else none.

Callers MUST request `expand=transitions.fields`: without it `fields` is absent, which is treated as "no fields", so the
required-fields refusal cannot see a screen's required fields.
`TransitionRefused.candidates` holds the sanitised names of the competing transitions for `ambiguous` (at most 10).
"""
import re
import unicodedata

MAX_TRANSITIONS = 200
MAX_RAW_NAME = 255  # a longer server name is treated as malformed
MAX_SHOWN = 80
MAX_CANDIDATES = 10
_ID_RE = re.compile(r"^[1-9][0-9]{0,9}\Z")
_CATEGORIES = frozenset({"new", "indeterminate", "done", "undefined"})  # Jira's four status category keys
_WANTED = {"active": "indeterminate", "done": "done"}

MESSAGES = {
    "none": "No matching transition. Set transitions.active or transitions.done in the config to a transition name.",
    "ambiguous": "More than one transition matches. Set transitions.active or transitions.done in the config to one "
                 "transition name.",
    "screen": "The transition needs a screen. It is not fired.",
    "conditional": "The transition is conditional. It is not fired.",
    "required-fields": "The transition has fields that must be filled in. It is not fired.",
    "unavailable": "The transition list is unusable or the transition is unavailable. Nothing is fired.",
    "not-done": "The configured transitions.done does not lead to a done status. It is not fired.",
}


def _clean(s):
    """Display form of untrusted server text: no control, format (bidi), surrogate, private-use or separator chars."""
    s = "".join(c for c in s if unicodedata.category(c)[0] != "C" and unicodedata.category(c) not in ("Zl", "Zp"))
    return s[:MAX_SHOWN]


class TransitionRefused(Exception):
    def __init__(self, code, candidates=()):
        super().__init__(MESSAGES[code])
        self.code = code
        self.candidates = tuple(candidates)


def _valid(t):
    """The fields `choose` uses, or None when the entry is malformed."""
    if not isinstance(t, dict):
        return None
    to = t.get("to")
    if not isinstance(to, dict):
        return None
    cat = to.get("statusCategory")
    if not isinstance(cat, dict):
        return None
    tid, name, to_id, to_name, key = t.get("id"), t.get("name"), to.get("id"), to.get("name"), cat.get("key")
    if not (isinstance(tid, str) and _ID_RE.match(tid) and isinstance(to_id, str) and _ID_RE.match(to_id)):
        return None
    if not (isinstance(name, str) and len(name) <= MAX_RAW_NAME and isinstance(to_name, str)
            and len(to_name) <= MAX_RAW_NAME and isinstance(key, str) and key in _CATEGORIES):
        return None
    for flag in ("hasScreen", "isConditional"):
        if not isinstance(t.get(flag), bool):
            return None
    if "isAvailable" in t and not isinstance(t["isAvailable"], bool):
        return None
    if "fields" in t and not isinstance(t["fields"], dict):
        return None
    return {"id": tid, "name": name, "to_id": to_id, "to_name": to_name, "category": key, "screen": t["hasScreen"],
            "conditional": t["isConditional"], "available": t.get("isAvailable", True),
            "fields_ok": all(isinstance(f, dict) and f.get("required") is False for f in t.get("fields", {}).values())}


def choose(transitions, current_status_id, want, names):
    if want not in _WANTED:
        raise ValueError("want must be 'active' or 'done'")
    if not (isinstance(transitions, list) and len(transitions) <= MAX_TRANSITIONS
            and isinstance(current_status_id, str) and _ID_RE.match(current_status_id)):
        raise TransitionRefused("unavailable")
    items = [_valid(t) for t in transitions]
    if any(i is None for i in items):
        raise TransitionRefused("unavailable")
    if len({i["id"] for i in items}) != len(items):
        raise TransitionRefused("ambiguous")
    if names is not None and not isinstance(names, dict):
        raise TransitionRefused("none")  # a malformed override is never read as "no override"
    configured = names.get(want) if names else None
    if configured is not None:
        # A configured name wins over the category. It must be a non-empty str and match exactly one transition other
        # than one that only reaches the current status (a no-op is not a transition).
        found = [i for i in items if i["to_id"] != current_status_id
                 and isinstance(configured, str) and configured and i["name"] == configured]
    else:
        # Category mode counts over the WHOLE list: excluding the current status first would turn a two-way ambiguity
        # (team-managed In Progress / In Review) into a unique, possibly backwards, pick (K3).
        found = [i for i in items if i["category"] == _WANTED[want]]
        if len(found) == 1 and found[0]["to_id"] == current_status_id:
            found = []  # already there: none (W3 treats that as a no-op)
    if not found:
        raise TransitionRefused("none")
    if len(found) > 1:
        raise TransitionRefused("ambiguous", [_clean(i["name"]) for i in found[:MAX_CANDIDATES]])
    c = found[0]
    for bad, code in ((not c["available"], "unavailable"), (c["screen"], "screen"), (c["conditional"], "conditional"),
                      (not c["fields_ok"], "required-fields")):
        if bad:
            raise TransitionRefused(code)
    return {"id": c["id"], "name": _clean(c["name"]), "to_id": c["to_id"], "to_name": _clean(c["to_name"])}


def choose_done(transitions, current_status_id, names):
    """`choose(..., "done", names)` for the verbs that close something (`done`, `push`): a configured name may pick a transition that
    does not close it ("To Do", "In Review"), so its target must be in the done category, else TransitionRefused("not-done")."""
    c = choose(transitions, current_status_id, "done", names)
    if next(t for t in transitions if t["id"] == c["id"])["to"]["statusCategory"]["key"] != "done":  # `choose` validated the list
        raise TransitionRefused("not-done")
    return c
