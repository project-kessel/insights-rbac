from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("management", "0087_alter_extrolerelation_ext_id_alter_exttenant_name"),
    ]

    operations = [
        migrations.AddField(
            model_name="auditlog",
            name="source",
            field=models.CharField(
                blank=True,
                choices=[("ai_assistant", "AI Assistant")],
                max_length=32,
                null=True,
            ),
        ),
    ]
