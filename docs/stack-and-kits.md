# Stack and kits

Strict mode has to run your tests, so it must know how. Two small files, both approved by a person, tell it:
`skj.stack.json` (your stack profile) and `skj.kits.lock.json` (the Spec Kit add-ons a person reviewed).
Everything here is local: nothing is sent to Jira.

## Your stack: `skj stack`

1. **Detect.** `skj stack detect` looks at the files committed in your repository and says which stacks it finds
   (for example Python with pytest) and why. It reads the last commit, never unsaved files.
2. **Start a draft.** `skj stack init --seed python-pytest` previews a draft profile from a built-in starting point.
   The seeds are `python-pytest`, `node-typescript` and `salesforce`. Your agent may also write a draft under `.skj/`
   and start from it with `skj stack init --from .skj/stack-draft.json`. Add `--apply` to write the draft.
   A draft is never trusted.
3. **Approve it, yourself.** `skj stack approve` shows every command the profile would run. If they are right, run
   `skj stack approve --apply` in your own terminal and type the code it prints. That marks the profile approved and sets
   `strict.test_command` in `skj.config.json`. It needs a real terminal, so an agent cannot do it for you.
4. **Check it.** `skj stack show` prints the profile's status. `skj stack show --check` fails in strict mode unless the
   profile is approved, matches `strict.test_command` and covers every stack it found.

## Your add-ons: `skj kits`

A kit is a Spec Kit add-on your agent uses, such as one for a particular language. Its prompts steer the agent, so a
changed kit should be looked at again.

- `skj kits verify` lists the installed kits and says whether each is pinned, changed or not pinned. It fails on any kit
  that is not pinned or changed since it was pinned.
- `skj kits pin <id>` previews the pin: file fingerprints, scan counts and a short checklist. After you have read the kit,
  run `skj kits pin <id> --apply` in your own terminal to record it in `skj.kits.lock.json`.

## Human-only approvals

`skj stack approve --apply` and `skj kits pin <id> --apply` are for people only. Your agent never runs them, and both refuse
without a real terminal. They are a record that a person looked, not a security lock: someone with write access to the
repository can still change files, which is why your code review matters. The full list of human-only actions is in
[the agent's rules](../rules/hitl.md).

## Checking the review rubric: `skj calibrate`

The agent's review step judges whether each test really checks its criterion. `skj calibrate --emit .skj/calibrate.md`
writes the rubric and 12 practice items for your AI model. Save the model's answer under `.skj/` and score it with
`skj calibrate --score .skj/calibrate-answer.json`: it passes at 10 of 12 with no false pass.
