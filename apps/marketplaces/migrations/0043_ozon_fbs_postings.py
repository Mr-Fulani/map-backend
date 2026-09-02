from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('marketplaces', '0042_ozon_offer_commerce_state')]
    operations = [
        migrations.CreateModel(
            name='OzonFbsPosting',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлено')),
                ('posting_number', models.CharField(max_length=100)),
                ('status', models.CharField(blank=True, max_length=100)),
                ('substatus', models.CharField(blank=True, max_length=100)),
                ('in_process_at', models.DateTimeField(blank=True, null=True)),
                ('shipment_date', models.DateTimeField(blank=True, null=True)),
                ('warehouse_id', models.CharField(blank=True, max_length=100)),
                ('products', models.JSONField(blank=True, default=list)),
                ('provider_updated_at', models.DateTimeField(blank=True, null=True)),
                ('last_synced_at', models.DateTimeField()),
                ('account', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='ozon_fbs_postings', to='marketplaces.marketplaceaccount')),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='ozon_fbs_postings', to='tenants.tenant')),
            ],
        ),
        migrations.AddConstraint(
            model_name='ozonfbsposting',
            constraint=models.UniqueConstraint(fields=('account', 'posting_number'), name='mkt_oz_fbs_posting_account_uniq'),
        ),
        migrations.AddIndex(
            model_name='ozonfbsposting',
            index=models.Index(fields=['tenant', 'account', '-in_process_at'], name='mkt_oz_fbs_tenant_idx'),
        ),
    ]
