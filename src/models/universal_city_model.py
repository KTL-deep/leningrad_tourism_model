class UniversalCityModel:
    """
    A class for aggregating all processed datasets within the Universal City Information Model.
    """

    def __init__(self):
        """
        Initializes the UniversalCityModel.
        """
        self.blocks = []
        self.osm_data = None
        self.regional_gis_data = None
        self.cultural_heritage_objects = None

    def add_blocks(self, blocks):
        """
        Adds generated spatial blocks to the model.

        :param blocks: A list of SpatialBlock objects.
        """
        self.blocks.extend(blocks)
        print(f"Added {len(blocks)} spatial blocks to the Universal City Model.")

    def set_osm_data(self, data):
        """
        Sets the OpenStreetMap data.

        :param data: The processed OSM data.
        """
        self.osm_data = data
        print("Set OSM data in the Universal City Model.")

    def set_regional_gis_data(self, data):
         """
         Sets the regional GIS data.

         :param data: The processed regional GIS data.
         """
         self.regional_gis_data = data
         print("Set regional GIS data in the Universal City Model.")

    def set_cultural_heritage_objects(self, data):
          """
          Sets the cultural heritage objects data.

          :param data: The processed cultural heritage objects data.
          """
          self.cultural_heritage_objects = data
          print("Set cultural heritage objects in the Universal City Model.")

    def get_summary(self):
        """
        Returns a summary of the Universal City Model.
        """
        return f"UniversalCityModel: {len(self.blocks)} Blocks, OSM Data: {'Yes' if self.osm_data is not None else 'No'}, Regional GIS Data: {'Yes' if self.regional_gis_data is not None else 'No'}, Cultural Heritage Objects: {'Yes' if self.cultural_heritage_objects is not None else 'No'}"
