import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mqtt_bambulab as mqtt
import print_history as history
from filament_usage_tracker import FilamentUsageTracker


class ActivePrintRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = patch.dict(history.db_config, db_path=str(Path(self.directory.name) / "history.db"))
        self.database.start()
        self.addCleanup(self.database.stop)
        history.create_database()
        for target in ("JOBS_3MF.enqueue", "FILAMENT_TRACKER", "_ensure_provisional_filament_usage"):
            mocked = patch("mqtt_bambulab." + target)
            mocked.start()
            self.addCleanup(mocked.stop)
        mqtt.ACTIVE_3MF_PRINTS.clear()
        mqtt.PRINTER_STATE = {}
        mqtt.PRINTER_STATE_LAST = {}
        mqtt.PENDING_PRINT_METADATA = {}
        self.report = {
            "command": "push_status", "gcode_state": "RUNNING", "print_type": "idle",
            "task_id": "0", "subtask_id": "0", "subtask_name": "same-model",
            "gcode_file": "same-model.gcode.3mf", "mc_percent": 12, "layer_num": 40,
        }

    def add_print(self, status, name="same-model"):
        print_id = history.insert_print(name, "local")
        history.ensure_layer_tracking(print_id, status)
        history.update_layer_tracking(print_id, printer_job_key="0:0")
        return print_id

    def test_running_report_after_restart_creates_history_before_download(self):
        previous = self.add_print("ABORTED")
        mqtt.processMessage({"print": dict(self.report)})
        current = history.get_latest_active_print_id()
        self.assertGreater(current, previous)
        self.assertEqual(history.get_layer_tracking_for_prints([current])[current]["status"], "RUNNING")
        self.assertEqual(history.get_layer_tracking_for_prints([previous])[previous]["status"], "ABORTED")
        mqtt.JOBS_3MF.enqueue.assert_called_once()
        mqtt.FILAMENT_TRACKER.begin_pending_print.assert_called_once()

    def test_restart_resumes_matching_active_attempt_without_duplicate(self):
        active = self.add_print("PAUSED", "renamed-model")
        history.update_layer_tracking(active, remote_3mf_path="/cache/same-model.gcode.3mf")
        mqtt.processMessage({"print": dict(self.report)})
        mqtt.processMessage({"print": {"command": "push_status", "mc_percent": 13}})
        self.assertEqual(history.get_latest_active_print_id(), active)
        mqtt.JOBS_3MF.enqueue.assert_called_once()

    def test_repeated_filename_and_zero_ids_create_distinct_attempts(self):
        mqtt.processMessage({"print": dict(self.report)})
        first = history.get_latest_active_print_id()
        history.update_layer_tracking(first, status="COMPLETED")
        mqtt.processMessage({"print": dict(self.report)})
        second = history.get_latest_active_print_id()
        self.assertGreater(second, first)
        self.assertEqual(history.get_layer_tracking_for_prints([first])[first]["status"], "COMPLETED")

    def test_zero_job_key_does_not_match_another_active_file(self):
        self.add_print("RUNNING", "another-model")
        self.assertIsNone(history.find_active_print_for_printer_job("same-model", job_key="0:0"))

    def test_metadata_enrichment_preserves_billed_consumption(self):
        active = self.add_print("RUNNING")
        history.insert_filament_usage(active, "PLA", "FFFFFF", 15, 1, length_used=1000, estimated_grams=20)
        history.insert_filament_usage(active, "PLA+", "FFFFFF", 0, 1, length_used=0)
        usage = history.get_all_filament_usage_for_print(active)[1]
        self.assertEqual(usage["grams_used"], 15)
        self.assertEqual(usage["length_used"], 1000)

    def test_previous_metadata_name_is_reused_for_same_printer_file(self):
        previous = self.add_print("ABORTED", "Pilz-Gross-Geteilt")
        history.update_layer_tracking(previous, remote_3mf_path="/model.gcode.3mf")
        connection = history.connect_database(history.db_config["db_path"])
        connection.execute(
            "UPDATE prints SET file_name_source = 'metadata' WHERE id = ?", (previous,)
        )
        connection.commit()
        connection.close()
        current = self.add_print("RUNNING", "Generated_Long_Model_Name")

        self.assertEqual(
            history.find_previous_metadata_print_name("/model.gcode.3mf", current),
            "Pilz-Gross-Geteilt",
        )
        self.assertIsNone(
            history.find_previous_metadata_print_name("/different.gcode.3mf", current)
        )

    def test_printer_status_persists_authoritative_end_time(self):
        print_id = self.add_print("RUNNING")
        history.update_printer_job_status(
            print_id, percent=19, status_at="2026-10-01 09:00:00",
            predicted_end_time="2026-10-01 18:20:00",
        )

        tracking = history.get_layer_tracking_for_prints([print_id])[print_id]
        self.assertEqual(tracking["printer_percent"], 19)
        self.assertEqual(tracking["predicted_end_time"], "2026-10-01 18:20:00")

    def test_terminal_zero_percent_uses_last_layer_checkpoint(self):
        print_id = self.add_print("RUNNING")
        history.update_layer_tracking(print_id, layers_printed=255, total_layers=1481)

        percent = history.set_printer_percent_from_layers(print_id)

        self.assertEqual(percent, 17.22)
        tracking = history.get_layer_tracking_for_prints([print_id])[print_id]
        self.assertEqual(tracking["printer_percent"], 17.22)

    def test_new_job_aborts_previous_active_history_entry(self):
        previous = self.add_print("RUNNING", "interrupted-model")

        changed = history.abort_active_prints_for_new_printer_job("2026-10-02 10:00:00")

        self.assertEqual(changed, 1)
        tracking = history.get_layer_tracking_for_prints([previous])[previous]
        self.assertEqual(tracking["status"], "ABORTED")
        self.assertEqual(tracking["actual_end_time"], "2026-10-02 10:00:00")

    def test_spool_usage_exposes_completion_details(self):
        print_id = history.insert_print("finished-model", "local", print_date="2026-10-01 08:00:00")
        history.ensure_layer_tracking(print_id, "COMPLETED")
        history.update_layer_tracking(print_id, actual_end_time="2026-10-01 09:35:00")
        history.insert_filament_usage(print_id, "PLA", "FFFFFF", 12.5, 1)
        history.update_filament_spool(print_id, 1, 42)

        usage = history.get_spool_print_usage(42)
        self.assertEqual(usage[0]["status"], "COMPLETED")
        self.assertEqual(usage[0]["actual_end_time"], "2026-10-01 09:35:00")
        self.assertEqual(usage[0]["duration_minutes"], 95.0)

    def test_cached_resume_skips_old_layers_and_restores_consumption(self):
        active = self.add_print("RUNNING")
        history.insert_filament_usage(active, "PLA", "FFFFFF", 15, 1, length_used=1000)
        tracker = FilamentUsageTracker()
        tracker.print_id = active
        def load_model(*args):
            tracker.active_model = {0: {}, 40: {}}
        with patch.object(tracker, "_load_model", side_effect=load_model), \
                patch.object(tracker, "_spend_filament_for_layer") as spend, \
                patch.object(tracker, "_bind_initial_spools"), \
                patch.object(tracker, "_maybe_update_predicted_total"), \
                patch.object(tracker, "_update_layer_tracking_progress"), \
                patch("filament_usage_tracker.clear_checkpoint"), \
                patch("filament_usage_tracker.save_checkpoint"), \
                patch("filament_usage_tracker._restore_thumbnail"), \
                patch("filament_usage_tracker.update_checkpoint_layer"):
            tracker._start_layer_tracking_for_model(
                model_path="missing-test-model", gcode_file_name=None, model_file_name=None,
                use_ams=False, ams_mapping=None, task_id=0, subtask_id=0, resume_layer=40,
            )
            spend.assert_not_called()
        self.assertEqual(tracker.cumulative_grams_used[1], 15)
        self.assertEqual(tracker.cumulative_length_used[1], 1000)
        self.assertIn(40, tracker.spent_layers)


if __name__ == "__main__":
    unittest.main()
