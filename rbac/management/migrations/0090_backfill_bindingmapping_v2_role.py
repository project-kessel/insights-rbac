import itertools

from django.db import migrations, transaction


def backfill_v2_role(apps, schema_editor):
    """Backfill v2_role FK from mappings JSON field."""
    BindingMapping = apps.get_model("management", "BindingMapping")
    RoleV2 = apps.get_model("management", "RoleV2")

    needed_uuids = set(
        BindingMapping.objects.filter(v2_role__isnull=True)
        .exclude(mappings__role__id=None)
        .values_list("mappings__role__id", flat=True)
        .distinct()
    )

    v2_role_lookup = {str(r.uuid): r.pk for r in RoleV2.objects.filter(uuid__in=needed_uuids).only("id", "uuid")}

    unprocessed_pks = BindingMapping.objects.filter(v2_role__isnull=True).values_list("pk", flat=True).iterator()

    for pk_chunk in itertools.batched(unprocessed_pks, 1000):
        with transaction.atomic():
            rows = BindingMapping.objects.filter(pk__in=pk_chunk, v2_role__isnull=True).select_for_update()

            batch = []
            for bm in rows:
                role_data = bm.mappings.get("role")
                if not role_data or "id" not in role_data:
                    continue

                role_uuid = str(role_data["id"])
                v2_role_pk = v2_role_lookup.get(role_uuid)
                if v2_role_pk is None:
                    v2 = RoleV2.objects.filter(uuid=role_uuid).values_list("pk", flat=True).first()
                    if v2 is None:
                        continue
                    v2_role_pk = v2
                    v2_role_lookup[role_uuid] = v2_role_pk

                bm.v2_role_id = v2_role_pk
                batch.append(bm)

            if batch:
                BindingMapping.objects.bulk_update(batch, ["v2_role_id"])


class Migration(migrations.Migration):

    dependencies = [
        ("management", "0089_bindingmapping_v2_role_and_indexes"),
    ]

    operations = [
        migrations.RunPython(backfill_v2_role, migrations.RunPython.noop),
    ]
