from django.db import migrations, models


def remove_invalid_service_account_principals(apps, schema_editor):
    """Remove the invalid service account principals

    Some of the service account principals were created when users hit the "/access" endpoint. Since the utils function
    that took care of the "get or create" principal was not prepared to store service account principals, it was
    storing those service accounts as "user" principals with a "service-account-${uuid}" username. The issue only
    affected stage because the service account changes weren't pushed to production yet, and that is why we thought
    that it was safe to simply remove those incorrect service account principals from the database.
    """
    Principal = apps.get_model("management", "Principal")

    invalid_service_account_principals = Principal.objects.filter(username__startswith="service-account-").filter(
        type="user"
    )
    invalid_service_account_principals.delete()


def update_service_accounts_usernames(apps, schema_editor):
    """Update the username for the service accounts

    The format in which the username comes in the "x-rh-identity" headers of the service accounts is the following one:
    "service-account-${client_id}. However, we were storing the usernames as they came in the payload that the UI sent
    us, and apparently they were grabbing the username from the IT service accounts' name field. This caused our RBAC
    users to not be able to fetch anything from the "/access" endpoint when using a service account because the
    usernames simply would not match. Even though this issue only affected stage since the service account changes
    weren't pushed to production, we decided that we rather fix the username for these service accounts.
    """
    Principal = apps.get_model("management", "Principal")

    service_account_principals = Principal.objects.filter(type="service-account")
    for sap in service_account_principals:
        service_account_id = sap.service_account_id

        sap.username = f"service-account-{service_account_id}"
        sap.save()


class Migration(migrations.Migration):

    dependencies = [
        ("management", "0041_service_account_support_ADR_036_RHCLOUD_27878"),
    ]

    operations = [
        migrations.RunPython(remove_invalid_service_account_principals),
        migrations.RunPython(update_service_accounts_usernames),
    ]
