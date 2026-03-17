class UniversalCityModel:
    """
    Класс агрегирующий все обработанные наборы данных в рамках Универсальной информационной модели города (UCM).
    """

    def __init__(self):
        """
        Инициализация Универсальной информационной модели города.
        """
        self.blocks = []
        self.osm_data = None
        self.regional_gis_data = None
        self.cultural_heritage_objects = None

    def add_blocks(self, blocks):
        """
        Добавляет сгенерированные пространственные блоки в модель.

        :param blocks: Список объектов SpatialBlock.
        """
        self.blocks.extend(blocks)
        print(f"Добавлено {len(blocks)} пространственных блоков в Универсальную информационную модель города (UCM).")

    def set_osm_data(self, data):
        """
        Устанавливает данные OpenStreetMap.

        :param data: Обработанные данные OSM.
        """
        self.osm_data = data
        print("Данные OSM добавлены в модель UCM.")

    def set_regional_gis_data(self, data):
         """
         Устанавливает региональные ГИС-данные.

         :param data: Обработанные региональные геоинформационные данные.
         """
         self.regional_gis_data = data
         print("Региональные ГИС-данные добавлены в модель UCM.")

    def set_cultural_heritage_objects(self, data):
          """
          Устанавливает данные об объектах культурного наследия.

          :param data: Обработанные данные ОКН.
          """
          self.cultural_heritage_objects = data
          print("Объекты культурного наследия добавлены в модель UCM.")

    def get_summary(self):
        """
        Возвращает сводку по Универсальной информационной модели города (UCM).
        """
        return f"Универсальная информационная модель (UCM): {len(self.blocks)} Блоков, OSM Данные: {'Да' if self.osm_data is not None else 'Нет'}, Региональные ГИС Данные: {'Да' if self.regional_gis_data is not None else 'Нет'}, Объекты культурного наследия: {'Да' if self.cultural_heritage_objects is not None else 'Нет'}"
