from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('marketplaces', '0037_ozon_offer_autofill'),
    ]

    operations = [
        migrations.AddField(
            model_name='ozonofferdraft',
            name='margin_pct',
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text='Пустое значение наследует правило выбранной категории Ozon.',
                max_digits=7,
                null=True,
                verbose_name='Индивидуальная наценка товара Ozon, %',
            ),
        ),
        migrations.AddField(
            model_name='ozonofferdraft',
            name='price_override',
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text='Точная цена выбранного товара и кабинета Ozon.',
                max_digits=12,
                null=True,
                verbose_name='Индивидуальная цена товара Ozon',
            ),
        ),
        migrations.AddConstraint(
            model_name='ozonofferdraft',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(margin_pct__isnull=True)
                    | models.Q(margin_pct__gt=-100)
                ),
                name='mkt_oz_offer_margin_positive',
            ),
        ),
        migrations.AddConstraint(
            model_name='ozonofferdraft',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(price_override__isnull=True)
                    | models.Q(price_override__gt=0)
                ),
                name='mkt_oz_offer_price_positive',
            ),
        ),
        migrations.AddConstraint(
            model_name='ozonofferdraft',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(margin_pct__isnull=True)
                    | models.Q(price_override__isnull=True)
                ),
                name='mkt_oz_offer_one_price_mode',
            ),
        ),
    ]
