import unittest
from unittest.mock import MagicMock, patch
from datetime import timedelta
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from dbpurge.management.commands.db_purge import Command
from tests.models import SampleRecord



class TestDBRetentionPolicy(unittest.TestCase):

    @patch('dbpurge.management.commands.db_purge.apps.get_model')
    def test_policy_validation(self, mock_get_model):
        # Valid policy
        valid_policy = {
            'app_name': 'mock_app',
            'model_name': 'MockModel',
            'time_based_column_name': 'timestamp',
            'data_retention_num_seconds': 86400 * 7 * 8  # 8 weeks
        }
        cmd = Command()
        cmd.validate_policy(valid_policy)  # No error should be raised

    @patch('dbpurge.management.commands.db_purge.apps.get_model')
    def test_expired_records_deletion(self, mock_get_model):
        # Mock model with expired records
        mock_model = MagicMock()
        mock_model.objects.filter.return_value = MagicMock(count=MagicMock(return_value=5))
        mock_get_model.return_value = mock_model

        policy = {
            'app_name': 'mock_app',
            'model_name': 'MockModel',
            'time_based_column_name': 'timestamp',
            'data_retention_num_seconds': 86400 * 7 * 8  # 8 weeks
        }
        cmd = Command()
        deleted_count = cmd.delete_expired_records(**policy)
        self.assertEqual(deleted_count, 5)

    @patch('dbpurge.management.commands.db_purge.apps.get_model')
    def test_expired_records_deletion_no_expired(self, mock_get_model):
        # Mock model with no expired records
        mock_model = MagicMock()
        mock_model.objects.filter.return_value = MagicMock(count=MagicMock(return_value=0))
        mock_get_model.return_value = mock_model

        policy = {
            'app_name': 'mock_app',
            'model_name': 'MockModel',
            'time_based_column_name': 'timestamp',
            'data_retention_num_seconds': 86400 * 7 * 8  # 8 weeks
        }
        cmd = Command()
        deleted_count = cmd.delete_expired_records(**policy)
        self.assertEqual(deleted_count, 0)

    @patch('dbpurge.management.commands.db_purge.apps.get_model')
    def test_nonexistent_time_based_column(self, mock_get_model):
        # Mock existing model
        mock_model = MagicMock()
        mock_model._meta.get_field.return_value = None
        mock_get_model.return_value = mock_model

        policy = {
            'app_name': 'mock_app',
            'model_name': 'MockModel',
            'time_based_column_name': 'non_existent_column',
            'data_retention_num_seconds': 86400 * 7 * 8  # 8 weeks
        }
        cmd = Command()
        with self.assertRaises(CommandError):
            cmd.validate_policy(policy)

class TestRetentionPoliciesFromSettings(TestCase):
    @override_settings(DB_PURGE_RETENTION_POLICIES=[
        {
            'app_name': 'tests',
            'model_name': 'SampleRecord',
            'time_based_column_name': 'created_at',
            'data_retention_num_seconds': 3600,
        },
    ])
    def test_deletes_only_records_past_the_configured_retention(self):
        stale = SampleRecord.objects.create(
            created_at=timezone.now() - timedelta(days=10), label='stale'
        )
        fresh = SampleRecord.objects.create(
            created_at=timezone.now(), label='fresh'
        )

        call_command('db_purge')

        self.assertFalse(SampleRecord.objects.filter(pk=stale.pk).exists())
        self.assertTrue(SampleRecord.objects.filter(pk=fresh.pk).exists())


if __name__ == '__main__':
    unittest.main()

