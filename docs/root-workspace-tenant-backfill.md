# Root workspace tenant backfills

The two one-time backfills update Kessel relationships for existing RBAC tenants:

1. `remove_legacy_root_workspace_tenant_parent_relations` removes the obsolete
   `workspace(root)#parent@tenant` tuple.
2. `replicate_root_workspace_tenants` adds `workspace(root)#tenant@tenant`.

Run them separately, in that order. New tenants already get the current
relationships at bootstrap. The jobs use the outbox and wait for the RBAC
consumer's PostgreSQL NOTIFY acknowledgement after each batch.

## Durable progress

`management_tenantbackfillprogress` has one row per job. It records the last
acknowledged tenant ID, the number scanned, and the highest tenant ID in the
initial scope. The removal job uses 500-tuple batches; the replication job uses
1,000-tuple batches. After the acknowledgement, each job advances its checkpoint
in PostgreSQL. A restarted pod resumes at `id > last_tenant_id`. If a pod stops
between acknowledgement and checkpoint update, it may repeat that batch.

The upper tenant ID is fixed when the checkpoint is first created. Tenants
created afterward are handled by normal bootstrap and do not extend a
multi-day backfill.

Check the checkpoint with a read-only SQL query:

```sql
SELECT job_name, last_tenant_id, processed_count, total_count, updated_at, completed_at
FROM management_tenantbackfillprogress
WHERE job_name = 'remove-legacy-root-workspace-tenant-parent';
```

The ClowdJobInvocation and Kubernetes Job must also report completion. A
checkpoint row alone is not proof that the pod completed successfully.

## Replacing a run started before checkpoints existed

The original production removal job has no durable cursor. Updating the RBAC
application does not change an already-running Job pod or its pinned image.
Coordinate its replacement separately; never run the old and new jobs together.
If the old removal job completes before this change is released, do not run it
again; proceed to the follow-up replication job instead.

To preserve its acknowledged work:

1. In the old pod logs, find a `received NOTIFY` line followed by
   `Processed N of ... tenants`. Use the last **acknowledged** batch you can
   verify, not an event that was only published or is still waiting.
2. Find the tenant organization ID in the last root-workspace cache log before
   that batch's `Publishing replication event` line. Query production RBAC
   through GABI to map it to `api_tenant.id`.
3. Check that the count of non-public tenants through that ID equals `N`.
   If the values do not match, choose an earlier verified batch or start the
   new job from zero. Never guess a cursor.
4. After the old Job has stopped and the new RBAC version and migration are
   deployed, seed the checkpoint once using the management command below.
   Then start a new ClowdJobInvocation with a new run number and the updated
   RBAC ref. Coordinate the app-interface change with AppSRE.

```sql
SELECT id FROM api_tenant WHERE org_id = '<logged-org-id>';
SELECT count(*) FROM api_tenant
WHERE tenant_name <> 'public' AND id <= <verified-tenant-id>;
```

```bash
/opt/rbac/rbac/manage.py seed_tenant_backfill_checkpoint \
  --job remove-legacy-root-workspace-tenant-parent \
  --after-tenant-id <verified-tenant-id> \
  --expected-processed-count <N>
```

The command rejects a seed if the ID and count differ from the database or if
a checkpoint already exists. It creates no relationship events. The new
removal job reads this row automatically. If there is no verified seed, omit
the seed command and let the new job create a checkpoint at the beginning.

The follow-up replication job uses the same checkpoint mechanism. Its first
production run needs no seed because it has not yet run there.

If a job fails, capture the failed pod's termination reason and logs before
Kubernetes removes it. The checkpoint prevents a full restart, but the failure
cause still needs investigation.
