from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0002_alter_user_created_at_alter_user_email_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='auth_version',
            field=models.PositiveIntegerField(
                default=1,
                help_text='Увеличивается при отзыве всех JWT-сессий.',
                verbose_name='Версия сессий',
            ),
        ),
    ]
