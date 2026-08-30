import apps.marketplaces.models
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('marketplaces', '0033_ozon_catalog_snapshots'),
        ('products', '0041_product_physical_profile'),
        ('tenants', '0016_webhook_delivery_claim_constraint'),
    ]

    operations = [
        migrations.CreateModel(
            name='OzonOfferDraft',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлено')),
                ('offer_id', models.CharField(default=apps.marketplaces.models.new_ozon_offer_id, editable=False, max_length=100, verbose_name='Стабильный Offer ID')),  # noqa: E501
                ('description_category_id', models.PositiveBigIntegerField(blank=True, null=True, verbose_name='ID категории Ozon')),  # noqa: E501
                ('type_id', models.PositiveBigIntegerField(blank=True, null=True, verbose_name='ID типа товара Ozon')),
                ('category_path', models.CharField(blank=True, max_length=1000, verbose_name='Путь категории Ozon')),
                ('type_name', models.CharField(blank=True, max_length=500, verbose_name='Тип товара Ozon')),
                ('tree_revision', models.CharField(blank=True, max_length=64)),
                ('account', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='ozon_offer_drafts', to='marketplaces.marketplaceaccount', verbose_name='Аккаунт Ozon')),  # noqa: E501
                ('product', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='ozon_offer_drafts', to='products.product', verbose_name='Товар')),  # noqa: E501
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='ozon_offer_drafts', to='tenants.tenant', verbose_name='Тенант')),  # noqa: E501
            ],
            options={
                'verbose_name': 'Черновик товара Ozon',
                'verbose_name_plural': 'Черновики товаров Ozon',
                'indexes': [
                    models.Index(
                        fields=['tenant', 'account', '-updated_at'],
                        name='mkt_oz_offer_tenant_idx',
                    ),
                ],
                'constraints': [
                    models.UniqueConstraint(
                        fields=('tenant', 'product', 'account'),
                        name='mkt_oz_offer_product_account_uniq',
                    ),
                    models.UniqueConstraint(
                        fields=('account', 'offer_id'),
                        name='mkt_oz_offer_identity_uniq',
                    ),
                ],
            },
        ),
    ]
