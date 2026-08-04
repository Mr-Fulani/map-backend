MEDIA_GENERATION_PROMPT_VERSION = 'product-media-v1'

BACKGROUND_STYLES = {
    'white_studio': 'чистый белый студийный фон для карточки товара',
    'light_gray_studio': 'светло-серый нейтральный студийный фон',
    'dark_studio': 'тёмный нейтральный студийный фон с мягким контрастом',
}


def build_product_media_prompt(product, background_style: str = 'white_studio') -> dict:
    if background_style not in BACKGROUND_STYLES:
        raise ValueError('Неизвестный стиль фона.')
    identity = ' '.join(filter(None, [product.brand, product.article, product.name]))
    return {
        'prompt_version': MEDIA_GENERATION_PROMPT_VERSION,
        'generation_prompt': (
            f'Сохрани товар без изменений: {identity}. Замени только окружение на '
            f'{BACKGROUND_STYLES[background_style]}. Сохрани форму, цвет, маркировку, '
            'отверстия, разъёмы и все конструктивные элементы товара. '
            'Естественная мягкая тень, реалистичная предметная фотография.'
        ),
        'negative_prompt': (
            'не изменять товар, не добавлять детали, крепёж, упаковку, текст, логотип, '
            'водяной знак, автомобиль, человека, руки или декоративные предметы'
        ),
        'background_style': background_style,
    }
