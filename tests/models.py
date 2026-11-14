from django.db import models


class SampleRecord(models.Model):
    created_at = models.DateTimeField()
    label = models.CharField(max_length=64)


class PolicyRecord(models.Model):
    """Toy model for MCP policy tests. Separate from SampleRecord on purpose."""

    status = models.CharField(max_length=32, default="new")
    level = models.IntegerField(default=0)
    score = models.FloatField(default=0.0)
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    created_at = models.DateTimeField()
    due_on = models.DateField()


class PolicyNote(models.Model):
    """To-many child of PolicyRecord, for joins in select and exclude."""

    record = models.ForeignKey(
        PolicyRecord, on_delete=models.CASCADE, related_name="notes"
    )
    body = models.CharField(max_length=64)
