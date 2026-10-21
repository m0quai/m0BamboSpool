import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import inventory_database


class InventoryDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database_path = Path(self.directory.name) / "inventory.db"
        self.path_patch = patch.object(inventory_database, "DATABASE_PATH", self.database_path)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)

    def test_initialize_restores_diameter_and_removes_legacy_density_column(self):
        connection = sqlite3.connect(self.database_path)
        connection.execute(
            """CREATE TABLE filaments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vendor_id INTEGER,
                material_id INTEGER,
                name TEXT NOT NULL,
                color_hex TEXT,
                density REAL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        connection.commit()
        connection.close()

        inventory_database.initialize()

        connection = sqlite3.connect(self.database_path)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(filaments)")}
        connection.close()
        self.assertIn("diameter", columns)
        self.assertNotIn("density", columns)

    def test_initialize_seeds_local_color_catalog(self):
        inventory_database.initialize()

        colors = inventory_database.list_colors()

        self.assertGreaterEqual(len(colors), 20)
        self.assertTrue(any(color["name"] == "Black" and color["hex_code"] == "000000" for color in colors))

    def test_new_filament_uses_default_diameter_when_no_value_is_supplied(self):
        filament_id = inventory_database.save_entity("filaments", {"name": "Test filament"})

        filament = next(item for item in inventory_database.list_filaments() if item["id"] == filament_id)

        self.assertEqual(filament["diameter"], 1.75)

    def test_booked_spool_keeps_its_filament_assignment(self):
        first_filament = inventory_database.save_entity("filaments", {"name": "First"})
        second_filament = inventory_database.save_entity("filaments", {"name": "Second"})
        spool_id = inventory_database.save_spool({"filament_id": first_filament})
        inventory_database.record_consumption(spool_id, weight_grams=2.5)

        spool = inventory_database.get_spool(spool_id)

        self.assertTrue(spool["filament_locked"])
        with self.assertRaisesRegex(ValueError, "gebuchten Spule"):
            inventory_database.update_spool_fields(spool_id, {"filament_id": second_filament})

    def test_spool_displays_missing_price_and_weights_from_filament(self):
        filament_id = inventory_database.save_entity("filaments", {
            "name": "Defaults", "weight": 1000, "spool_weight": 130, "price": 11,
        })
        spool_id = inventory_database.save_spool({"filament_id": filament_id})

        spool = inventory_database.get_spool(spool_id)

        self.assertEqual(spool["initial_weight"], 1000)
        self.assertEqual(spool["spool_weight"], 130)
        self.assertEqual(spool["price"], 11)
        self.assertEqual(
            spool["filament_defaults"],
            {"initial_weight": True, "spool_weight": True, "price": True},
        )
        inventory_database.update_spool_fields(spool_id, {"price": 12})
        self.assertNotIn("price", inventory_database.get_spool(spool_id)["filament_defaults"])
        inventory_database.update_spool_fields(spool_id, {"price": 0})
        defaulted_again = inventory_database.get_spool(spool_id)
        self.assertEqual(defaulted_again["price"], 11)
        self.assertIn("price", defaulted_again["filament_defaults"])

    def test_inventory_accepts_german_decimal_commas(self):
        filament_id = inventory_database.save_entity("filaments", {
            "name": "Komma", "diameter": "1,75", "price": "12,50",
        })
        inventory_database.update_filament_fields(filament_id, {"weight": "1000,25"})
        spool_id = inventory_database.save_spool({"filament_id": filament_id, "initial_weight": "850,50"})

        filament = next(item for item in inventory_database.list_filaments() if item["id"] == filament_id)
        spool = inventory_database.get_spool(spool_id)
        self.assertEqual(filament["diameter"], 1.75)
        self.assertEqual(filament["price"], 12.5)
        self.assertEqual(filament["weight"], 1000.25)
        self.assertEqual(spool["initial_weight"], 850.5)

    def test_empty_spool_value_uses_an_empty_filament_default(self):
        filament_id = inventory_database.save_entity("filaments", {"name": "No values"})
        spool_id = inventory_database.save_spool({"filament_id": filament_id})

        spool = inventory_database.get_spool(spool_id)

        self.assertEqual(spool["spool_weight"], 0.0)
        self.assertEqual(spool["filament"]["spool_weight"], 0.0)
        self.assertIn("spool_weight", spool["filament_defaults"])

    def test_archived_spool_can_only_be_unarchived(self):
        filament_id = inventory_database.save_entity("filaments", {
            "name": "Archived", "weight": 1000, "spool_weight": 130, "price": 11,
        })
        spool_id = inventory_database.save_spool({"filament_id": filament_id})

        inventory_database.update_spool_fields(spool_id, {"archived": True})
        with self.assertRaisesRegex(ValueError, "archiviert und gesperrt"):
            inventory_database.update_spool_fields(spool_id, {"price": 12})

        inventory_database.update_spool_fields(spool_id, {"archived": False})
        self.assertFalse(inventory_database.get_spool(spool_id)["archived"])


if __name__ == "__main__":
    unittest.main()
