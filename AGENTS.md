# Repository execution rules

Before changing Avito feed code, read:

- docs/AVITO_FEED_STATUS.md;
- docs/AVITO_FEED_ROADMAP.md;
- docs/AVITO_FEED_CHANGESET_MANIFEST.md;
- docs/ENGINEERING_EXECUTION_RULES.md.

Before changing Ozon code, read docs/OZON_STATUS.md. Ozon is implemented and
live; that document does not authorize provider writes, enabling automation or
new paid integrations.

## Active freeze

Avito feed feature development is frozen. Do not add or continue cleanup,
P7 retention, GC, object deletion, new Avito feed migrations, new modes, worker
wiring or activation work unless the user explicitly activates that roadmap
package in a new request. The already enabled P6 private serving is the current
production path and is not itself frozen; do not extend it. The freeze covers
Avito feed only: Ozon migrations in `apps/marketplaces` are outside it.

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
  new lines excluding tests, two consecutive migrations, two independent
  subsystems, or adds a new queue, settings mode or external integration
  (docs/ENGINEERING_EXECUTION_RULES.md, section 4, is authoritative).
- Do not mark work complete without the exact test commands and results.
- Keep production feed settings at the exact contract in docs/DEPLOYMENT.md
  (currently lifecycle `dual_write`, run `durable`, ingress `dual_write`,
  artifact `active`, storage `stable_bridge`, empty cutover allowlist) unless a
  separately approved rollout explicitly changes them. The legacy values in
  `.env.example` are local defaults, not production.
- Shared files such as marketplace models, tasks and services must be split by
  diff hunk; never assign the entire file to a later package for convenience.
