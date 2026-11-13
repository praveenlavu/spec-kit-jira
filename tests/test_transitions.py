import copy
import inspect
import time

import pytest
from fakejira import STATUSES, TRANSITIONS

from skj import transitions
from skj.transitions import TransitionRefused, choose

ALL_STATES = {n: sid for n, (sid, _cat) in STATUSES.items()}  # To Do 10000, In Progress 3, In Review 10001, Done 10002


def T(tid, name, to=None, **over):
    """One transition in the shape GET /issue/{key}/transitions serves (fictional ids as in the team-managed spike)."""
    to = to or name
    sid, cat = STATUSES.get(to, ("10009", "indeterminate"))
    t = {"id": tid, "name": name, "to": {"self": "https://example-site.atlassian.net/rest/api/3/status/" + sid,
                                          "description": "", "iconUrl": "", "name": to, "id": sid,
                                          "statusCategory": {"id": 1, "key": cat, "colorName": "x", "name": "x"}},
         "hasScreen": False, "isGlobal": True, "isInitial": False, "isAvailable": True, "isConditional": False,
         "isLooped": False}
    t.update(over)
    return t


def team_managed():
    return [T(tid, name) for tid, name in TRANSITIONS]  # ids and statuses come from the fake, so they cannot drift


def refusal(*args):
    with pytest.raises(TransitionRefused) as e:
        choose(*args)
    return e.value


def test_done_unique_on_team_managed_fixture():
    got = choose(team_managed(), "10000", "done", None)  # the issue is To Do
    assert got == {"id": "41", "name": "Done", "to_id": "10002", "to_name": "Done"}
    for name in ("To Do", "In Progress", "In Review"):  # unique from every not-done state
        assert choose(team_managed(), ALL_STATES[name], "done", {"active": None, "done": None})["id"] == "41", name
    assert refusal(team_managed(), "10002", "done", None).code == "none"  # already done


def test_active_ambiguous_without_config():
    for name, cur in ALL_STATES.items():  # ambiguous from every state, never a (possibly backwards) pick (K3)
        e = refusal(team_managed(), cur, "active", {"active": None, "done": None})
        assert e.code == "ambiguous", name
        assert refusal(team_managed(), cur, "active", None).code == "ambiguous", name


def test_category_mode_independent_of_current_status():
    # property: apart from "the one candidate is the current status -> none", the result never depends on the status
    for ts in (team_managed(), team_managed()[:3], team_managed() + [T("51", "Close", to="Done")],
               [T("11", "To Do"), T("41", "Done")], [T("21", "In Progress")]):
        for want in ("active", "done"):
            outcomes = {}
            for cur in ("10000", "3", "10001", "10002", "777"):
                try:
                    outcomes[cur] = choose(ts, cur, want, None)["id"]
                except TransitionRefused as e:
                    outcomes[cur] = e.code
            picks = {v for v in outcomes.values() if v not in ("none",)}
            assert len(picks) <= 1, (want, outcomes)
            if "none" in outcomes.values() and len(picks) == 1:  # none only where the unique candidate is the status
                (pick,) = picks
                if pick.isdigit():
                    t = next(x for x in ts if x["id"] == pick)
                    assert [c for c, v in outcomes.items() if v == "none"] == [t["to"]["id"]], (want, outcomes)


def test_configured_name_resolves():
    got = choose(team_managed(), "10000", "active", {"active": "In Review", "done": None})
    assert (got["id"], got["to_id"]) == ("31", "10001")
    # a configured name wins over the category (it need not be in the wanted category)
    assert choose(team_managed(), "10000", "done", {"active": None, "done": "In Review"})["id"] == "31"


def test_screen_refused():
    ts = team_managed()
    ts[3]["hasScreen"] = True
    assert refusal(ts, "10000", "done", None).code == "screen"
    assert refusal(ts, "10000", "done", {"done": "Done"}).code == "screen"  # a configured name does not bypass it


def test_conditional_refused():
    ts = team_managed()
    ts[3]["isConditional"] = True
    assert refusal(ts, "10000", "done", None).code == "conditional"
    assert refusal(ts, "10000", "done", {"done": "Done"}).code == "conditional"


def test_unavailable_refused():
    ts = team_managed()
    ts[3]["isAvailable"] = False
    assert refusal(ts, "10000", "done", None).code == "unavailable"
    assert refusal(ts, "10000", "done", {"done": "Done"}).code == "unavailable"


def test_required_fields_refused():
    for fields in ({"resolution": {"required": True}}, {"x": {"required": "yes"}}, {"x": {}}, {"x": "junk"},
                   {"a": {"required": False}, "b": {"required": True}}):
        ts = team_managed()
        ts[3]["fields"] = fields
        assert refusal(ts, "10000", "done", None).code == "required-fields", fields
        assert refusal(ts, "10000", "done", {"done": "Done"}).code == "required-fields", fields
    for fine in ({}, {"a": {"required": False}}):  # empty or optional-only fields are fine; an absent key is the default
        ts = team_managed()
        ts[3]["fields"] = fine
        assert choose(ts, "10000", "done", None)["id"] == "41"


def test_current_status_excluded():
    # the only done-category transition targets the current status: no candidate left
    assert refusal(team_managed(), "10002", "done", None).code == "none"
    # a configured name that targets the current status is refused too (a no-op is not a transition)
    assert refusal(team_managed(), "10002", "done", {"done": "Done"}).code == "none"
    # category mode counts over the whole list: a second done-category transition makes it ambiguous from every state
    ts = team_managed() + [T("51", "Close", to="Done")]
    for cur in ("10000", "10002"):
        assert refusal(ts, cur, "done", None).code == "ambiguous"
    # name mode excludes the current status: of two same-named transitions only the one elsewhere counts
    ts = team_managed() + [T("52", "Finish", to="Done"), T("53", "Finish", to="In Review")]
    assert choose(ts, "10002", "done", {"done": "Finish"})["id"] == "53"
    assert refusal(ts, "10000", "done", {"done": "Finish"}).code == "ambiguous"


def test_resolution_ignored():
    ts = team_managed()
    for t in ts:
        t["resolution"] = {"name": "Won't Do", "id": "999"}
        t["to"]["resolution"] = 12345
    assert choose(ts, "10000", "done", None)["id"] == "41"
    for junk in (None, "garbage", [], {"x": object()}, 1 / 3):
        ts2 = copy.deepcopy(team_managed())
        ts2[3]["resolution"] = junk
        assert choose(ts2, "10000", "done", None)["id"] == "41"
    assert "resolution" not in inspect.getsource(transitions).lower()  # no read of it, no mention, in the module


def test_configured_name_no_match_or_two_matches():
    assert refusal(team_managed(), "10000", "active", {"active": "Nope"}).code == "none"
    two = team_managed() + [T("32", "In Review", to="Done")]
    assert refusal(two, "10000", "active", {"active": "In Review"}).code == "ambiguous"
    # exact string match only: no case folding, no trimming
    for n in ("in review", "IN REVIEW", " In Review", "In Review "):
        assert refusal(team_managed(), "10000", "active", {"active": n}).code == "none", n
    assert choose(team_managed(), "10000", "active", {"active": "In Review"})["id"] == "31"
    assert refusal(team_managed(), "10000", "done", {"done": ""}).code == "none"
    blank = team_managed() + [T("51", "", to="Done")]  # a transition named "" is never matched by an empty name
    assert refusal(blank, "10000", "done", {"done": ""}).code == "none"
    assert refusal(blank, "10000", "active", {"active": ""}).code == "none"
    assert refusal(team_managed(), "10000", "done", {"done": 5}).code == "none"


def test_no_transitions_at_all_is_none():
    assert refusal([], "10000", "done", None).code == "none"  # e.g. no Transition issues permission: the list is empty


def bad_lists():
    base = team_managed
    yield "not a list", None
    yield "dict", {"transitions": []}
    yield "tuple", tuple(base())
    yield "string", "[]"
    yield "non-dict entry", base() + ["x"]
    yield "none entry", base() + [None]
    for k in ("id", "name", "to", "hasScreen", "isConditional"):
        ts = base()
        del ts[0][k]
        yield "missing " + k, ts
    for k, v in (("id", 11), ("id", "0"), ("id", "01"), ("id", "12345678901"), ("id", "1 "), ("id", "١١"), ("id", None),
                 ("id", "1\n"), ("name", 5), ("name", None), ("name", "n" * 300), ("to", "Done"), ("to", []),
                 ("hasScreen", "false"), ("hasScreen", 0), ("hasScreen", None), ("isConditional", "no"),
                 ("isConditional", 1), ("isAvailable", "true"), ("isAvailable", None), ("fields", []),
                 ("fields", "x"), ("fields", None)):
        ts = base()
        ts[1][k] = v
        yield "%s=%r" % (k, v), ts
    for path, v in ((("to", "id"), 3), (("to", "id"), "x"), (("to", "name"), None), (("to", "statusCategory"), None),
                    (("to", "statusCategory"), "done"), (("to", "statusCategory", "key"), "weird"),
                    (("to", "statusCategory", "key"), None), (("to", "statusCategory", "key"), ["done"])):
        ts = base()
        node = ts[1]
        for p in path[:-1]:
            node = node[p]
        node[path[-1]] = v
        yield "to." + ".".join(path[1:]) + "=%r" % (v,), ts
    ts = base()
    del ts[1]["to"]["statusCategory"]
    yield "missing statusCategory", ts


@pytest.mark.parametrize("label,ts", list(bad_lists()), ids=[x[0] for x in bad_lists()])
def test_malformed_input_refuses_never_crashes(label, ts):
    # any malformed entry refuses the whole list (even the entry that would not be picked), with a documented code
    for want in ("active", "done"):
        for names in (None, {"active": "In Review", "done": "Done"}):
            assert refusal(ts, "10000", want, names).code == "unavailable"


def test_malformed_current_status_and_names():
    for cur in (None, 3, "", "abc", "0", "10000\n"):
        assert refusal(team_managed(), cur, "done", None).code == "unavailable", cur
    for names in ("Done", ["Done"], 5):
        assert refusal(team_managed(), "10000", "done", names).code == "none"
    with pytest.raises(ValueError):
        choose(team_managed(), "10000", "closed", None)


def test_unknown_but_valid_category_is_not_a_candidate():
    ts = team_managed()
    ts[2]["to"]["statusCategory"]["key"] = "undefined"  # Jira's fourth category
    assert choose(ts, "10000", "active", None)["id"] == "21"


def test_duplicate_ids_refused():
    ts = team_managed() + [T("41", "Done again", to="Done")]
    assert refusal(ts, "10000", "done", None).code == "ambiguous"
    assert refusal(ts, "10000", "done", {"done": "Done"}).code == "ambiguous"  # even a name that is unique


def test_more_than_200_entries_refused():
    big = [T(str(i), "t%d" % i, to="Done") for i in range(1, 202)]
    assert refusal(big, "10000", "done", None).code == "unavailable"
    assert refusal(big[:200], "10000", "done", None).code == "ambiguous"  # exactly 200 is still read


def test_linear_time_on_200_entries():
    ts = [T(str(i), "t%d" % i, to="In Progress") for i in range(1, 200)] + [T("500", "Finish", to="Done")]
    t0 = time.monotonic()
    for _ in range(200):
        assert choose(ts, "10000", "done", None)["id"] == "500"
        assert choose(ts, "10000", "active", {"active": "t77"})["id"] == "77"
    assert time.monotonic() - t0 < 5


HOSTILE = "Do‮ne​\x1b[31m\nX \ud800" + "z" * 200


def test_hostile_names_are_sanitised_everywhere():
    ts = team_managed()
    ts.append(T("61", HOSTILE, to="Done"))  # a second done-category transition: ambiguous, candidates carry the name
    e = refusal(ts, "10000", "done", None)
    assert e.code == "ambiguous"
    shown = " ".join([str(e), repr(e)] + list(e.candidates))
    for bad in ("‮", "​", "\x1b", "\n", " ", "\ud800", ""):
        assert bad not in shown
    assert all(len(c) <= 80 for c in e.candidates)
    # the message is fixed per code: it does not echo a server name at all
    assert "Done" not in str(e) and "zzz" not in str(e)
    # a returned transition carries sanitised display names; ids stay validated digits
    ok = choose([T("62", HOSTILE, to="Done")], "10000", "done", None)
    assert ok["id"] == "62" and len(ok["name"]) <= 80
    assert all(c not in ok["name"] for c in ("‮", "\x1b", "\n"))


def test_messages_fixed_per_code():
    seen = {}
    for ts, cur, want, names in (([], "10000", "done", None), (team_managed(), "10000", "active", None),
                                 (None, "10000", "done", None)):
        e = refusal(ts, cur, want, names)
        seen.setdefault(e.code, set()).add(str(e))
    assert all(len(v) == 1 for v in seen.values())
    assert set(transitions.MESSAGES) == {"none", "ambiguous", "screen", "conditional", "required-fields", "unavailable", "not-done"}
    assert len(set(transitions.MESSAGES.values())) == 7


def test_input_not_mutated():
    ts = team_managed()
    before = copy.deepcopy(ts)
    choose(ts, "10000", "done", None)
    assert ts == before
