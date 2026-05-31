"""Base auto parts taxonomy used to bootstrap platform and tenant catalogs.

This is our own compact market taxonomy. It is intentionally not a verbatim
copy of TecDoc, Autodoc, Exist, HELLA, Bosch or any other supplier catalog.
"""

BASE_PART_CATEGORY_TREE = [
    {
        'name': 'Тормозная система',
        'aliases': ['Brakes', 'Тормоза'],
        'children': [
            ('Тормозные колодки', ['Колодки', 'Brake pads'], True),
            ('Тормозные диски', ['Диски тормозные', 'Brake discs'], True),
            ('Тормозные барабаны', ['Барабаны тормозные'], True),
            ('Тормозные суппорты', ['Суппорт', 'Caliper'], True),
            ('Тормозные шланги', ['Шланг тормозной'], True),
            ('Главные тормозные цилиндры', ['ГТЦ'], True),
            ('Датчики ABS', ['ABS sensor', 'Датчик АБС'], True),
        ],
    },
    {
        'name': 'Подвеска и рулевое управление',
        'aliases': ['Suspension', 'Steering', 'Ходовая часть'],
        'children': [
            ('Амортизаторы', ['Стойки амортизатора', 'Shock absorbers'], True),
            ('Пружины подвески', ['Пружины'], True),
            ('Рычаги подвески', ['Рычаг', 'Control arms'], True),
            ('Шаровые опоры', ['Шаровая'], True),
            ('Сайлентблоки', ['Втулки рычагов'], True),
            ('Стойки стабилизатора', ['Тяги стабилизатора'], True),
            ('Рулевые тяги и наконечники', ['Наконечник рулевой', 'Tie rods'], True),
            ('Ступицы и подшипники', ['Подшипник ступицы', 'Wheel bearing'], True),
            ('Рулевые рейки', ['Рейка рулевая'], True),
        ],
    },
    {
        'name': 'Двигатель',
        'aliases': ['Engine', 'Мотор'],
        'children': [
            ('Поршневая группа', ['Поршни', 'Кольца поршневые'], True),
            ('Прокладки двигателя', ['Прокладка ГБЦ', 'Gaskets'], True),
            ('Опоры двигателя', ['Подушка двигателя'], True),
            ('Масляные насосы', ['Насос масляный'], True),
            ('Водяные насосы', ['Помпа', 'Water pump'], True),
            ('Термостаты', ['Thermostat'], True),
            ('Турбокомпрессоры', ['Турбина', 'Turbocharger'], True),
            ('Датчики двигателя', ['ДМРВ', 'ДПКВ', 'ДПРВ'], True),
        ],
    },
    {
        'name': 'Фильтры',
        'aliases': ['Filters', 'Фильтра'],
        'children': [
            ('Масляные фильтры', ['Фильтр масляный', 'Oil filter'], True),
            ('Воздушные фильтры', ['Фильтр воздушный', 'Air filter'], True),
            ('Салонные фильтры', ['Фильтр салона', 'Cabin filter'], True),
            ('Топливные фильтры', ['Фильтр топливный', 'Fuel filter'], True),
            ('Фильтры АКПП', ['Фильтр трансмиссии'], True),
        ],
    },
    {
        'name': 'Ремни и приводные элементы',
        'aliases': ['Belts', 'Timing drive'],
        'children': [
            ('Ремни ГРМ', ['Ремень ГРМ', 'Timing belt'], True),
            ('Цепи ГРМ', ['Цепь ГРМ', 'Timing chain'], True),
            ('Комплекты ГРМ', ['Комплект ремня ГРМ'], True),
            ('Ролики и натяжители', ['Натяжитель', 'Ролик ремня'], True),
            ('Приводные ремни', ['Ремень генератора', 'Поликлиновой ремень'], True),
        ],
    },
    {
        'name': 'Топливная система',
        'aliases': ['Fuel system'],
        'children': [
            ('Топливные насосы', ['Бензонасос', 'Fuel pump'], True),
            ('Форсунки', ['Инжектор', 'Injector'], True),
            ('Регуляторы давления топлива', ['Регулятор давления'], True),
            ('Топливные рампы', ['Рампа топливная'], True),
            ('Дроссельные узлы', ['Дроссель', 'Throttle body'], True),
        ],
    },
    {
        'name': 'Охлаждение, отопление и кондиционер',
        'aliases': ['Cooling', 'Heating', 'AC', 'Thermal management'],
        'children': [
            ('Радиаторы охлаждения', ['Радиатор двигателя'], True),
            ('Вентиляторы охлаждения', ['Вентилятор радиатора'], True),
            ('Расширительные бачки', ['Бачок расширительный'], True),
            ('Радиаторы отопителя', ['Печка', 'Heater core'], True),
            ('Компрессоры кондиционера', ['Компрессор A/C'], True),
            ('Конденсоры кондиционера', ['Радиатор кондиционера'], True),
            ('Испарители кондиционера', ['Испаритель A/C'], True),
        ],
    },
    {
        'name': 'Электрика и зажигание',
        'aliases': ['Electrical', 'Ignition'],
        'children': [
            ('Аккумуляторы', ['АКБ', 'Battery'], True),
            ('Генераторы', ['Alternator'], True),
            ('Стартеры', ['Starter'], True),
            ('Свечи зажигания', ['Spark plugs'], True),
            ('Катушки зажигания', ['Ignition coils'], True),
            ('Реле и предохранители', ['Реле', 'Предохранители'], False),
            ('Лампы автомобильные', ['Автолампы', 'Bulbs'], True),
        ],
    },
    {
        'name': 'Трансмиссия и сцепление',
        'aliases': ['Transmission', 'Clutch'],
        'children': [
            ('Комплекты сцепления', ['Сцепление', 'Clutch kit'], True),
            ('Маховики', ['Flywheel'], True),
            ('ШРУСы и приводы', ['ШРУС', 'Приводной вал', 'CV joint'], True),
            ('Карданные валы', ['Кардан'], True),
            ('Опоры КПП', ['Подушка коробки'], True),
            ('Детали коробки передач', ['КПП', 'АКПП', 'МКПП'], True),
        ],
    },
    {
        'name': 'Кузов, стекла и оптика',
        'aliases': ['Body', 'Lighting'],
        'children': [
            ('Фары', ['Передняя оптика', 'Headlights'], True),
            ('Фонари', ['Задние фонари', 'Tail lights'], True),
            ('Зеркала', ['Зеркало боковое'], True),
            ('Бамперы', ['Бампер'], True),
            ('Крылья', ['Крыло'], True),
            ('Капоты', ['Капот'], True),
            ('Двери', ['Дверь'], True),
            ('Стекла', ['Лобовое стекло', 'Автостекло'], True),
            ('Замки и ручки', ['Замок двери', 'Ручка двери'], True),
        ],
    },
    {
        'name': 'Выхлопная система',
        'aliases': ['Exhaust'],
        'children': [
            ('Глушители', ['Глушитель', 'Muffler'], True),
            ('Катализаторы', ['Каталитический нейтрализатор'], True),
            ('Лямбда-зонды', ['Датчик кислорода', 'Oxygen sensor'], True),
            ('Клапаны EGR', ['ЕГР', 'EGR valve'], True),
            ('Сажевые фильтры', ['DPF', 'Фильтр сажевый'], True),
        ],
    },
    {
        'name': 'Стеклоочистители и омыватель',
        'aliases': ['Wipers', 'Washer system'],
        'children': [
            ('Щетки стеклоочистителя', ['Дворники', 'Wiper blades'], True),
            ('Моторы стеклоочистителя', ['Мотор дворников'], True),
            ('Насосы омывателя', ['Моторчик омывателя'], True),
            ('Форсунки омывателя', ['Жиклер омывателя'], True),
        ],
    },
    {
        'name': 'Колеса и шины',
        'aliases': ['Wheels', 'Tyres'],
        'children': [
            ('Шины', ['Резина', 'Tyres'], True),
            ('Диски колесные', ['Колесные диски', 'Rims'], True),
            ('Датчики давления шин', ['TPMS'], True),
            ('Крепеж колес', ['Болты колесные', 'Гайки колесные'], False),
        ],
    },
    {
        'name': 'Масла, жидкости и автохимия',
        'aliases': ['Fluids', 'Chemicals'],
        'fitment_required': False,
        'children': [
            ('Моторные масла', ['Engine oil'], False),
            ('Трансмиссионные масла', ['Gear oil', 'ATF'], False),
            ('Тормозные жидкости', ['Brake fluid'], False),
            ('Антифризы', ['Охлаждающая жидкость', 'Coolant'], False),
            ('Смазки и очистители', ['Автохимия'], False),
        ],
    },
    {
        'name': 'Крепеж, инструмент и аксессуары',
        'aliases': ['Accessories', 'Tools', 'Fasteners'],
        'fitment_required': False,
        'children': [
            ('Крепеж универсальный', ['Болты', 'Гайки', 'Клипсы'], False),
            ('Инструмент', ['Автоинструмент'], False),
            ('Аксессуары салона', ['Коврики', 'Чехлы'], True),
            ('Багажные системы', ['Багажник на крышу'], True),
        ],
    },
]


def normalize_category_name(name: str) -> str:
    return ''.join(char for char in name.lower() if char.isalnum())


def iter_base_part_categories():
    for root in BASE_PART_CATEGORY_TREE:
        yield {
            'name': root['name'],
            'parent': '',
            'aliases': root.get('aliases', []),
            'fitment_required': root.get('fitment_required', True),
        }
        for child_name, aliases, fitment_required in root.get('children', []):
            yield {
                'name': child_name,
                'parent': root['name'],
                'aliases': aliases,
                'fitment_required': fitment_required,
            }
