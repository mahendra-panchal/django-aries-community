# Generated by Django 2.2.10 on 2020-03-06 13:44

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('aries_community', '0002_auto_20200301_1443'),
    ]

    operations = [
        migrations.AddField(
            model_name='agentconversation',
            name='status',
            field=models.CharField(blank=True, max_length=80),
        ),
    ]
