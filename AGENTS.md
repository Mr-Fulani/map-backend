# Repository execution rules

Before changing Avito feed code, read:

- docs/AVITO_FEED_STATUS.md;
- docs/AVITO_FEED_ROADMAP.md;
- docs/AVITO_FEED_CHANGESET_MANIFEST.md;
- docs/ENGINEERING_EXECUTION_RULES.md.

## Active freeze

Avito feed feature development is frozen. Do not add or continue cleanup,
0039, private serving, GC, object deletion, new migrations, new modes, worker
wiring or activation work unless the user explicitly activates that roadmap
package in a new request.

Allowed work during the freeze:

- documentation and inventory;
- splitting existing changes into the declared P0–P7 packages;
- read-only review;
- running tests;
- fixing only failures proven by tests in the currently activated package.

## Scope control

- Work on one roadmap package at a time.
- Do not implement findings that belong to a later package; record them in the
  backlog instead.
- Stop and ask for a new plan if a change exceeds 20 production files, 1,500
  new lines, two migrations, or two independent subsystems.
- Do not mark work complete without the exact test commands and results.
- Keep production feed settings at legacy/legacy/disabled/legacy_public unless
  a separately approved rollout explicitly changes them.
- Shared files such as marketplace models, tasks and services must be split by
  diff hunk; never assign the entire file to a later package for convenience.
