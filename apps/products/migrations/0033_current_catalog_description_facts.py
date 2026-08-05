import hashlib
import re

from django.db import migrations, models
from django.db.models import Count


CATALOG_SOURCES = ['tachka', 'rossko', 'euroauto']


def _description_hint(value):
    normalized = ' '.join(str(value or '').split())
    markers = [
        match.start()
        for pattern in (
            r'\bкросс[ -]?коды?\b',
            r'\bOEM(?:\s*/\s*Cross)?[ -]?коды?\b',
            r'\bподходит\s+для\s+следующих\s+модификаций\b',
            r'\bприменяемость\s*:',
        )
        if (match := re.search(pattern, normalized, re.IGNORECASE))
    ]
    if markers:
        normalized = normalized[:min(markers)].strip(' .,:;-')
    return normalized[:3000]


def clean_catalog_description_facts(apps, schema_editor):
    Fact = apps.get_model('products', 'ProductEnrichmentFact')
    catalogue = Fact.objects.filter(
        source_id__in=CATALOG_SOURCES,
        fact_type='description_hint',
    )
    duplicate_groups = (
        catalogue
        .values('tenant_id', 'product_id', 'source_id', 'fact_type', 'name')
        .annotate(total=Count('id'))
        .filter(total__gt=1)
    )
    for group in duplicate_groups.iterator():
        identity = {
            key: group[key]
            for key in ('tenant_id', 'product_id', 'source_id', 'fact_type', 'name')
        }
        facts = list(Fact.objects.filter(**identity))
        winner = max(
            facts,
            key=lambda fact: (
                fact.last_seen_at or fact.updated_at or fact.created_at,
                fact.updated_at,
                fact.pk,
            ),
        )
        Fact.objects.filter(**identity).exclude(pk=winner.pk).delete()

    for fact in catalogue.filter(name='description').iterator():
        cleaned = _description_hint(fact.value)
        if not cleaned:
            fact.delete()
            continue
        if cleaned == fact.value:
            continue
        fact.value = cleaned
        fact.value_hash = hashlib.sha256(cleaned.strip().encode()).hexdigest()
        fact.save(update_fields=['value', 'value_hash', 'updated_at'])


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0032_productparsejob_source_offer'),
    ]

    operations = [
        migrations.RunPython(clean_catalog_description_facts, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='productenrichmentfact',
            constraint=models.UniqueConstraint(
                condition=(
                    models.Q(fact_type='description_hint')
                    & models.Q(source_id__in=CATALOG_SOURCES)
                ),
                fields=('tenant', 'product', 'source_id', 'fact_type', 'name'),
                name='unique_current_catalog_description_fact',
            ),
        ),
    ]
