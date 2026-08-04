import hashlib
from dataclasses import dataclass

from apps.ai_agent.models import AIPromptTemplate, AITaskType
from apps.ai_agent.prompts import (
    DESCRIPTION_OUTPUT_SCHEMA,
    DESCRIPTION_PROMPT_VERSION,
    GENERIC_DESCRIPTION_PROMPT_VERSION,
    GENERIC_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
)


@dataclass(frozen=True)
class PromptSelection:
    system_prompt: str
    output_schema: dict
    version: str
    key: str
    template: AIPromptTemplate | None = None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.system_prompt.encode('utf-8')).hexdigest()


def resolve_description_prompt(product, marketplace: str = 'avito') -> PromptSelection:
    domain = _product_catalog_domain(product)
    scopes = [
        (domain, marketplace),
        (domain, ''),
        ('', marketplace),
        ('', ''),
    ]
    for catalog_domain, marketplace_scope in scopes:
        template = AIPromptTemplate.objects.filter(
            task_type=AITaskType.DESCRIPTION,
            catalog_domain=catalog_domain,
            marketplace=marketplace_scope,
            is_active=True,
        ).order_by('-version').first()
        if template:
            return PromptSelection(
                system_prompt=template.system_prompt,
                output_schema=template.output_schema or DESCRIPTION_OUTPUT_SCHEMA,
                version=f'db-v{template.version}',
                key=f'{template.task_type}:{catalog_domain or "*"}:{marketplace_scope or "*"}',
                template=template,
            )
    is_auto_parts = domain == 'auto_parts'
    return PromptSelection(
        system_prompt=SYSTEM_PROMPT if is_auto_parts else GENERIC_SYSTEM_PROMPT,
        output_schema=DESCRIPTION_OUTPUT_SCHEMA,
        version=(
            DESCRIPTION_PROMPT_VERSION
            if is_auto_parts else GENERIC_DESCRIPTION_PROMPT_VERSION
        ),
        key=f'{AITaskType.DESCRIPTION}:{domain or "*"}:{marketplace}',
    )


def _product_catalog_domain(product) -> str:
    category = getattr(product, 'catalog_category', None)
    root_domain = getattr(category, 'root_domain', None) if category else None
    return str(
        getattr(root_domain, 'slug', '')
        or getattr(product.tenant, 'catalog_domain', '')
        or ''
    )
