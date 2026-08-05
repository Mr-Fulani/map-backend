from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0031_restore_web_search_images_for_review'),
    ]

    operations = [
        migrations.AddField(
            model_name='productparsejob',
            name='source_availability',
            field=models.CharField(
                choices=[
                    ('unknown', 'Наличие не указано'),
                    ('in_stock', 'В наличии'),
                    ('preorder', 'Под заказ'),
                    ('out_of_stock', 'Нет в наличии'),
                ],
                default='unknown',
                max_length=20,
                verbose_name='Наличие в источнике',
            ),
        ),
        migrations.AddField(
            model_name='productparsejob',
            name='source_availability_text',
            field=models.CharField(
                blank=True,
                max_length=200,
                verbose_name='Текст наличия в источнике',
            ),
        ),
        migrations.AddField(
            model_name='productparsejob',
            name='source_currency',
            field=models.CharField(default='RUB', max_length=3, verbose_name='Валюта источника'),
        ),
        migrations.AddField(
            model_name='productparsejob',
            name='source_price',
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=14,
                null=True,
                verbose_name='Цена в источнике',
            ),
        ),
        migrations.AddField(
            model_name='productparsejob',
            name='source_price_is_from',
            field=models.BooleanField(default=False, verbose_name='Цена указана «от»'),
        ),
        migrations.AddField(
            model_name='productparsejob',
            name='source_quantity',
            field=models.PositiveIntegerField(
                blank=True,
                null=True,
                verbose_name='Количество в источнике',
            ),
        ),
    ]
