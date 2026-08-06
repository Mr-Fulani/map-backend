import re
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
    fitment_presentation: dict = field(default_factory=dict)
    catalog_number_presentation: dict = field(default_factory=dict)
    content_profile: dict = field(default_factory=dict)
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
            'fitment_presentation': self.fitment_presentation,
            'catalog_number_presentation': self.catalog_number_presentation,
            'content_profile': self.content_profile,
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
        trusted_fact_count = sum(1 for fact in facts if should_auto_apply_record(fact))

        self._add_attributes(context, attributes)
        self._add_cross_codes(context, cross_codes)
        self._add_fitments(context, fitments)
        self._add_facts(context, facts)
        context.content_profile = self._build_content_profile(
            product=product,
            attributes=attributes,
            cross_codes=cross_codes,
            fitments=context.trusted_fitments,
            trusted_fact_count=trusted_fact_count,
        )
        return context

    @staticmethod
    def _build_content_profile(
        *, product, attributes, cross_codes, fitments, trusted_fact_count,
    ) -> dict:
        available_sections = []
        if fitments:
            available_sections.append('compatibility')
        if attributes:
            available_sections.append('specifications')
        if cross_codes:
            available_sections.append('catalog_numbers')
        if trusted_fact_count:
            available_sections.append('verified_facts')
        if getattr(product, 'condition', ''):
            available_sections.append('condition')

        enrichment_sections = [
            section for section in available_sections
            if section not in {'condition'}
        ]
        evidence_count = (
            len(attributes) + len(cross_codes) + len(fitments) + trusted_fact_count
        )
        if len(enrichment_sections) >= 3 and evidence_count >= 8:
            level = 'rich'
            target = {'min': 600, 'max': 2200}
        elif enrichment_sections:
            level = 'standard'
            target = {'min': 300, 'max': 1400}
        else:
            level = 'sparse'
            target = {'min': 180, 'max': 700}
        return {
            'level': level,
            'available_sections': available_sections,
            'evidence_count': evidence_count,
            'target_description_chars': target,
            'do_not_pad': True,
        }

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
        context.catalog_number_presentation = self._build_catalog_number_presentation(
            cross_codes,
        )

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
        presentation_builder = ProductAIEnrichmentContextBuilder._build_fitment_presentation
        context.fitment_presentation = presentation_builder(trusted_fitments)

    @staticmethod
    def _build_fitment_presentation(fitments) -> dict:
        """Build a compact, truthful marketplace view without losing raw fitments."""
        vehicles = []
        seen_vehicles = set()
        grouped_models = {}
        for item in fitments:
            make = ' '.join(str(item.make or '').split())
            model = ' '.join(str(item.model or '').split())
            generation = ' '.join(str(item.generation or '').split())
            vehicle_key = (make.casefold(), model.casefold(), generation.casefold())
            if vehicle_key not in seen_vehicles:
                seen_vehicles.add(vehicle_key)
                vehicles.append({
                    'make': make,
                    'model': model,
                    'generation': generation,
                })
            make_key = make.casefold()
            make_group = grouped_models.setdefault(make_key, {
                'make': make,
                'models': [],
                '_seen_models': set(),
            })
            model_family = ProductAIEnrichmentContextBuilder._model_family(make, model)
            model_key = model_family.casefold()
            if model_family and model_key not in make_group['_seen_models']:
                make_group['_seen_models'].add(model_key)
                make_group['models'].append(model_family)

        compact = len(fitments) > 6 or len(vehicles) > 6
        groups = []
        model_budget = 8
        for group in grouped_models.values():
            models = group['models']
            visible_models = models[:model_budget]
            model_budget -= len(visible_models)
            groups.append({
                'make': group['make'],
                'models': visible_models,
                'remaining_models_count': max(0, len(models) - len(visible_models)),
            })
        return {
            'mode': 'compact' if compact else 'detailed',
            'confirmed_fitment_count': len(vehicles),
            'evidence_record_count': len(fitments),
            'unique_vehicle_count': len(vehicles),
            'vehicles': vehicles if not compact else [],
            'groups': groups,
            'required_makes': [group['make'] for group in groups if group['make']],
            'required_models': [
                model
                for group in groups
                for model in group['models']
            ],
        }

    @staticmethod
    def _model_family(make: str, model: str) -> str:
        """Collapse body styles to a buyer-recognisable model family when safe."""
        if 'MERCEDES' in make.upper():
            class_match = re.match(r'^([A-Z]+-CLASS)\b', model, flags=re.IGNORECASE)
            if class_match:
                return class_match.group(1).upper()
        return model

    @staticmethod
    def _build_catalog_number_presentation(cross_codes) -> dict:
        """Collapse formatting aliases for buyers while preserving raw search data."""
        groups = {}
        for item in cross_codes:
            manufacturer = ' '.join(str(item.manufacturer or '').split())
            code = ' '.join(str(item.code or '').split())
            normalized = ''.join(character for character in code.upper() if character.isalnum())
            identity = normalized
            if (
                'MERCEDES' in manufacturer.upper()
                and normalized.startswith('A')
                and normalized[1:].isdigit()
            ):
                identity = normalized[1:]
            key = (manufacturer.casefold(), identity)
            current = groups.get(key)
            # Mercedes numbers with the conventional A prefix are clearer to buyers.
            if current is None or (normalized.startswith('A') and not current['code'].upper().startswith('A')):
                groups[key] = {
                    'manufacturer': manufacturer,
                    'code': code,
                    'code_type': item.code_type,
                }

        numbers = list(groups.values())
        return {
            'label': 'Номера для поиска и проверки совместимости',
            'numbers': numbers[:8],
            'total_unique_count': len(numbers),
            'remaining_count': max(0, len(numbers) - 8),
        }

    @staticmethod
    def _add_facts(context: ProductAIEnrichmentContext, facts) -> None:
        trusted_facts = []
        seen_facts = set()
        for fact in facts:
            if should_auto_apply_record(fact):
                identity = (
                    fact.fact_type,
                    fact.name.strip().casefold(),
                    ' '.join(fact.value.split()).casefold(),
                )
                if identity in seen_facts:
                    continue
                seen_facts.add(identity)
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
