from django.test import TestCase
from django.utils import timezone

from dbpurge.management.commands.purge_mcp_server import get_purge_candidates
from tests.models import SampleRecord


class TestListPurgeCandidates(TestCase):
    def test_includes_toy_model_date_columns(self):
        candidates = get_purge_candidates()

        self.assertIn("tests", candidates)
        self.assertEqual(candidates["tests"]["samplerecord"], ["created_at"])

    def test_read_only(self):
        SampleRecord.objects.create(created_at=timezone.now(), label="x")

        get_purge_candidates()

        self.assertEqual(SampleRecord.objects.count(), 1)
