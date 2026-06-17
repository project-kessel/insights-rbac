from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("management", "0086_alter_auditlog_resource_type_add_role_binding"),
    ]

    operations = [
        migrations.AlterField(
            model_name="extrolerelation",
            name="ext_id",
            field=models.CharField(max_length=64),
        ),
        migrations.AlterField(
            model_name="exttenant",
            name="name",
            field=models.CharField(max_length=64, unique=True),
        ),
    ]
