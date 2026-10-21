from django.db import migrations


def normalize_blank_tenant_identifiers(apps, schema_editor):
    """Convert blank-string account_id/org_id values to NULL.

    Some tenants had '' persisted instead of NULL, which crashes consumers
    that treat these nullable CharField as integers (e.g. TenantSerializer).
    """
    Tenant = apps.get_model("api", "Tenant")
    Tenant.objects.filter(account_id="").update(account_id=None)


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0018_tenant_org_config"),
    ]

    operations = [
        migrations.RunPython(normalize_blank_tenant_identifiers, migrations.RunPython.noop),
    ]
