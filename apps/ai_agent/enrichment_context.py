from dataclasses import dataclass, field

from apps.products.models import ProductEnrichmentFact
from apps.products.source_policy import should_auto_apply_fitment, should_auto_apply_record


_NON_VEHICLE_CROSS_MANUFACTURERS = {
    'ATE', 'BOSCH', 'BREMBO', 'DELPHI', 'FEBI', 'FERODO', 'GATES', 'HELLA',
    'INA', 'KYB', 'LEMFORDER', 'LUK', 'MANN-FILTER', 'MAHLE', 'MEYLE',
    'MONROE', 'NGK', 'SACHS', 'SKF', 'TEXTAR', 'TRW', 'VALEO',
}


@dataclass
class ProductAIEnrichmentContext:
    """Единый набор enrichment-фактов для AI-описания товара."""

    trusted_lines: list[str] = field(default_factory=list)
    cautious_lines: list[str] = field(default_factory=list)
    trusted_fitments: list[dict] = field(default_factory=list)
    cautious_vehicle_makes: list[str] = field(default_factory=list)
    excluded_review_count: int = 0

    @property
    def has_context(self) -> bool:
        return bool(self.trusted_lines or self.cautious_lines or self.excluded_review_count)

    def to_prompt_lines(self) -> list[str]:
        lines = []
        if self.trusted_lines:
            lines.append('Проверенные факты:')
            lines.extend(self.trusted_lines)
        if self.cautious_lines:
            lines.append('Осторожные факты:')
            lines.extend(self.cautious_lines)
        if self.excluded_review_count:
            lines.append(
                f'Исключено спорных фактов: {self.excluded_review_count}. '
                'Не используй их в описании.'
            )
        return lines

    def to_prompt_payload(self) -> dict:
        return {
            'trusted_facts': self.trusted_lines,
            'cautious_facts': self.cautious_lines,
            'trusted_fitments': self.trusted_fitments,
            'cautious_vehicle_makes': self.cautious_vehicle_makes,
            'excluded_review_count': self.excluded_review_count,
        }


class ProductAIEnrichmentContextBuilder:
    """Собирает trusted/cautious/review факты из tenant-scoped enrichment данных."""

    def build(self, product) -> ProductAIEnrichmentContext:
        context = ProductAIEnrichmentContext()
        attributes = list(product.attributes.all().order_by('name')[:12])
        cross_codes = list(product.cross_codes.all().order_by('manufacturer', 'code')[:20])
        fitments = list(product.fitments.all().order_by('make', 'model', 'generation')[:50])
        facts = list(product.enrichment_facts.all().order_by('fact_type', 'name')[:30])

        self._add_attributes(context, attributes)
        self._add_cross_codes(context, cross_codes)
        self._add_fitments(context, fitments)
        self._add_facts(context, facts)
        return context

    @staticmethod
    def _add_attributes(context: ProductAIEnrichmentContext, attributes) -> None:
        if not attributes:
            return
        values = '; '.join(f'{item.name}: {item.value}' for item in attributes)
        context.trusted_lines.append(f'Характеристики: {values}')

    def _add_cross_codes(self, context: ProductAIEnrichmentContext, cross_codes) -> None:
        if not cross_codes:
            return
        vehicle_makes = self.extract_vehicle_makes_from_cross_codes(cross_codes)
        if vehicle_makes:
            context.cautious_vehicle_makes.extend(vehicle_makes)
            context.cautious_lines.append(
                'Вероятные марки авто по OEM/Cross: '
                + ', '.join(vehicle_makes)
            )
        values = '; '.join(
            f'{item.manufacturer}: {item.code}' if item.manufacturer else item.code
            for item in cross_codes
        )
        context.trusted_lines.append(f'OEM/Cross-коды: {values}')

    @staticmethod
    def _add_fitments(context: ProductAIEnrichmentContext, fitments) -> None:
        trusted_fitments = []
        for fitment in fitments:
            if should_auto_apply_fitment(fitment):
                trusted_fitments.append(fitment)
            else:
                context.excluded_review_count += 1
        if not trusted_fitments:
            return

        values = []
        for item in trusted_fitments:
            context.trusted_fitments.append({
                'make': item.make,
                'model': item.model,
                'generation': item.generation,
                'date_from': item.date_from,
                'date_to': item.date_to,
                'modification': item.modification,
                'engine_code': item.engine_code,
                'power_hp': item.power_hp,
            })
            parts = [
                item.make,
                item.model,
                item.generation,
                '-'.join(filter(None, [item.date_from, item.date_to])),
                item.modification,
                item.engine_code,
                f'{item.power_hp} л.с.' if item.power_hp else '',
            ]
            values.append(' '.join(part for part in parts if part))
        context.trusted_lines.append(f'Подходит к автомобилям: {"; ".join(values)}')

    @staticmethod
    def _add_facts(context: ProductAIEnrichmentContext, facts) -> None:
        trusted_facts = []
        for fact in facts:
            if should_auto_apply_record(fact):
                trusted_facts.append(fact)
            else:
                context.excluded_review_count += 1
        if not trusted_facts:
            return

        technical = [
            fact for fact in trusted_facts
            if fact.fact_type == ProductEnrichmentFact.FactType.TECHNICAL
        ]
        hints = [
            fact for fact in trusted_facts
            if fact.fact_type == ProductEnrichmentFact.FactType.DESCRIPTION_HINT
        ]
        warnings = [
            fact for fact in trusted_facts
            if fact.fact_type == ProductEnrichmentFact.FactType.WARNING
        ]
        if technical:
            context.trusted_lines.append('Технические факты: ' + '; '.join(
                f'{fact.name}: {fact.value}' for fact in technical[:8]
            ))
        if hints:
            context.trusted_lines.append('Подсказки для описания: ' + '; '.join(
                f'{fact.name}: {fact.value}' for fact in hints[:8]
            ))
        if warnings:
            context.cautious_lines.append('Предупреждения источника: ' + '; '.join(
                f'{fact.name}: {fact.value}' for fact in warnings[:5]
            ))

    @staticmethod
    def extract_vehicle_makes_from_cross_codes(cross_codes) -> list[str]:
        makes = []
        for item in cross_codes:
            manufacturer = (item.manufacturer or '').strip()
            if not manufacturer:
                continue
            parts = [
                part.strip(' .')
                for part in manufacturer.replace('&', '/').split('/')
            ]
            for part in parts:
                normalized = part.upper()
                if (
                    len(normalized) < 2
                    or normalized in _NON_VEHICLE_CROSS_MANUFACTURERS
                    or normalized in makes
                ):
                    continue
                makes.append(normalized)
        return makes[:8]
