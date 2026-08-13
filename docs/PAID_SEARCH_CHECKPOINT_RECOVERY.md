# Recovery of paid-search checkpoints

Image search and Euroauto parsing persist provider responses before local
database/S3 apply. If local apply repeatedly fails until the canonical durable
dispatch is exhausted, an operator can revive that **same owner** without
issuing a second paid provider request:

```bash
python manage.py resume_paid_search_checkpoint \
  --image-task-id 123 --confirm image:123

python manage.py resume_paid_search_checkpoint \
  --euroauto-job-id 456 --confirm euroauto:456
```

The command is intentionally fail-closed. It accepts only a failed canonical
dispatch and either an encrypted successful/empty checkpoint waiting for local
apply, or a plan containing only proven safe/pre-send failures. `STARTED`,
`OUTCOME_UNCERTAIN`, unresolved reconciliation, already applied, successful,
cancelled, mismatched and arbitrary dispatches are rejected. Resolve uncertain
provider outcomes with the reconciliation command instead; this recovery
command never changes provider accounting evidence.
