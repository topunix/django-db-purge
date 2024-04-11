import logging
import time
from django.core.management.base import BaseCommand
from django.core.management import CommandError
from django.apps import apps
from datetime import timedelta
from django.utils import timezone

logger = logging.getLogger(__name__)

EIGHT_WEEKS_IN_SECS = 86400 * 7 * 8

class Command(BaseCommand):
    help = 'Delete expired database records based on the retention policy'

    def handle(self, *args, **options):
        total_deleted_records = 0
        retention_policies = [
            {
                'app_name': 'django_app_name_here',
                'model_name': 'YourDjangoModelNameHere',
                'time_based_column_name': 'timestamp',
                'data_retention_num_seconds': EIGHT_WEEKS_IN_SECS,
            },
            # Add more retention policies as needed
        ]

        for policy in retention_policies:
            self.validate_policy(policy)
            deleted_records = self.delete_expired_records(**policy)
            total_deleted_records += deleted_records

        self.stdout.write(self.style.SUCCESS(f"Total number of deleted records: {total_deleted_records}"))

    def validate_policy(self, policy):
        app_name = policy.get('app_name')
        model_name = policy.get('model_name')
        time_based_column_name = policy.get('time_based_column_name')
        data_retention_num_seconds = policy.get('data_retention_num_seconds')

         # Check if app_name and model_name are valid
        try:
           model = apps.get_model(app_label=app_name, model_name=model_name)
        except LookupError:
           raise CommandError(f"Model '{model_name}' in app '{app_name}' not found")

        # Check if time_based_column_name exists in the model's fields
        if not model._meta.get_field(time_based_column_name):
            raise CommandError(f"Time-based column '{time_based_column_name}' does not exist in model '{model_name}'.")

        # Check if data_retention_num_seconds is a positive integer
        if not isinstance(data_retention_num_seconds, int) or data_retention_num_seconds <= 0:
            raise CommandError("data_retention_num_seconds must be a positive integer.")


    def delete_expired_records(self, app_name, model_name, time_based_column_name, data_retention_num_seconds):
        logger.info(
          'Checking for %s %s DB records to delete which are '
          'beyond retention period...',
          app_name,
          model_name,
        )
        model = apps.get_model(app_label=app_name, model_name=model_name)
        start_time = time.time()
        min_age_records_to_delete = timezone.now() - timedelta(seconds=data_retention_num_seconds)
        logger.info(
            'Executing delete query on %s %s records older than %s...',
            app_name,
            model_name,
            min_age_records_to_delete,
        )
        expired_records = model.objects.filter(
            **{f"{time_based_column_name}__lte": min_age_records_to_delete}
        )
        deleted_count = expired_records.count()
        expired_records.delete()
        took = time.time() - start_time
        logger.info(
            'Done deleting %s %s DB records to delete which are '
            'beyond retention period. Took %s secs. Result: %s',
            app_name,
            model_name,
            took,
            expired_records,
        )
        return deleted_count
