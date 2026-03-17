from shapely.geometry import Polygon

class SpatialBlock:
    """
    A class representing a spatial block, which is the smallest indivisible cell of spatial analysis.
    It attributes information about services, buildings, and functional purposes to the polygon.
    """

    def __init__(self, block_id, polygon):
        """
        Initializes the SpatialBlock.

        :param block_id: An identifier for the spatial block.
        :param polygon: A Shapely Polygon representing the block's geometry.
        """
        self.block_id = block_id
        self.polygon = polygon
        self.services = []
        self.buildings = []
        self.functional_purpose = None

    def attribute_services(self, gdf_services):
        """
        Attributes the spatial block with information about services located within its polygon.

        :param gdf_services: A GeoDataFrame containing service points.
        """
        print(f"Attributing services to block {self.block_id}")
        # Placeholder logic: Check if services intersect with the block's polygon
        # and store relevant information.
        for index, row in gdf_services.iterrows():
            if self.polygon.contains(row['geometry']):
                self.services.append(row['service_type'])
                print(f"Service {row['service_type']} added.")

    def attribute_buildings(self, gdf_buildings):
         """
         Attributes the spatial block with information about existing buildings within its polygon.

         :param gdf_buildings: A GeoDataFrame containing building footprints.
         """
         print(f"Attributing buildings to block {self.block_id}")
         # Placeholder logic: Check if buildings intersect with the block's polygon
         # and store their functional purpose.
         for index, row in gdf_buildings.iterrows():
             if self.polygon.intersects(row['geometry']):
                 self.buildings.append({
                     'building_id': row.get('id', 'unknown'),
                     'functional_purpose': row.get('building_type', 'unknown')
                 })
                 print(f"Building {row.get('id', 'unknown')} added.")

    def __str__(self):
        """
        Returns a string representation of the spatial block.
        """
        return f"SpatialBlock(ID: {self.block_id}, Services: {len(self.services)}, Buildings: {len(self.buildings)}, Functional Purpose: {self.functional_purpose})"
